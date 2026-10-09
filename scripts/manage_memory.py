"""Local Memory edit/archive/restore; mutations default to read-only dry-run."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for source in (PROJECT_ROOT, PROJECT_ROOT / "backend/src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from kulai_memory.application.lifecycle import MemoryLifecycleError, MemoryRevisionConflictError
from kulai_memory.application.memory import MemoryArchivedError
from kulai_memory.automatic_indexing import AutomaticMemoryIndexer
from kulai_memory.database_safety import (
    database_config, database_config_for_database, database_host_is_loopback,
    database_snapshot_url, require_owned_database, run_database_doctor,
    validate_backup_output, validate_restore_source,
)
from scripts.embedding_smoke import probe_embedding
from kulai_memory.lifecycle_persistence import change_memory
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.settings import get_settings
from scripts import db_backup, db_restore_smoke


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        super().error("Invalid arguments. Use --help for the command contract.")


def _revision(value):
    parsed = int(value)
    if parsed < 1:
        raise ValueError
    return parsed


def parser():
    cli = _SafeParser(description=__doc__)
    actions = cli.add_subparsers(dest="action", required=True, parser_class=_SafeParser)
    for name in ("show", "edit", "archive", "restore"):
        command = actions.add_parser(name)
        command.add_argument("--memory-id", type=UUID, required=True)
        if name == "edit":
            command.add_argument("--expected-revision", type=_revision, required=True)
            command.add_argument("--content-file", type=Path, required=True)
        if name != "show":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true")
            mode.add_argument("--execute", action="store_true")
            command.add_argument("--confirm-main-memory-write", action="store_true")
            command.add_argument("--backup-output", type=Path)
    return cli


async def _backup_and_verify(output, *, owned, config):
    if owned is None:
        size, digest, _ = await db_backup.create_backup(output, force=False)
        restored = await db_restore_smoke.restore_smoke(output)
    else:
        size, digest, _ = await db_backup.create_owned_database_backup(output, force=False, owned=owned, config=config)
        restored = await db_restore_smoke.restore_owned_database_backup(output, owned=owned, config=config)
    if size <= 0 or output.stat().st_size != size or db_backup.sha256_file(output) != digest:
        raise MemoryLifecycleError()
    print(f"backup.size={size}\nbackup.sha256={digest}\nrestore_smoke=PASS")
    return restored


async def run(args, *, owned=None, source_config=None):
    """The optional test target is a checked ownership capability, never a CLI flag."""
    settings = get_settings()
    configured = source_config or database_config()
    if owned is not None:
        validate_restore_source(configured, app_env=settings.app_env)
        await require_owned_database(owned, config=configured)
        config = database_config_for_database(owned.name, config=configured)
    else:
        config = configured
    engine = indexer = None
    try:
        execute = bool(getattr(args, "execute", False))
        if execute and (not args.confirm_main_memory_write or args.backup_output is None):
            raise MemoryLifecycleError()
        if execute and owned is None and settings.app_env.casefold() not in {"dev", "development", "local"}:
            raise MemoryLifecycleError()
        if not database_host_is_loopback(config):
            raise MemoryLifecycleError()
        if settings.kulai_embedding_model != "bge-m3:567m-fp16" or settings.kulai_vector_dimension != 1024:
            raise MemoryLifecycleError()
        if not (await run_database_doctor(async_url=config.async_url)).ok:
            raise MemoryLifecycleError()
        output = validate_backup_output(args.backup_output, force=False) if execute else None
        content = None
        if args.action == "edit":
            try:
                content = args.content_file.read_text(encoding="utf-8")
                if not content.strip():
                    raise ValueError
            except (OSError, ValueError):
                raise MemoryLifecycleError() from None
        engine = create_async_engine(config.async_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            try:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                memory = await PostgresMemoryRepository(db=session).get_by_id(args.memory_id)
            finally:
                await session.rollback()
        if memory is None:
            from kulai_memory.application.lifecycle import MemoryNotFoundError
            raise MemoryNotFoundError()
        print(f"memory_id={memory.id}\nrevision={memory.revision}\narchived={str(memory.archived_at is not None).lower()}")
        if args.action == "show":
            print(f"content={memory.content}")  # Explicit user's local diagnostic.
            return 0
        if args.action == "edit":
            if memory.archived_at is not None:
                raise MemoryArchivedError()
            if memory.revision != args.expected_revision:
                raise MemoryRevisionConflictError()
        if not execute:
            print(f"mode=dry-run\naction={args.action}\nstatus=DRY_RUN_OK")
            return 0
        before = await database_snapshot_url(config.async_url)
        if args.action != "archive":
            indexer = AutomaticMemoryIndexer(settings=settings, session_factory=factory)
            probe = await asyncio.wait_for(probe_embedding(provider=indexer.provider, expected_dimension=1024), timeout=120)
            if probe.model_id != "bge-m3:567m-fp16":
                raise MemoryLifecycleError()
        await _backup_and_verify(output, owned=owned, config=configured)
        if await database_snapshot_url(config.async_url) != before:
            raise MemoryLifecycleError()
        result = await change_memory(action=args.action, memory_id=memory.id, session_factory=factory,
            indexer=indexer, content=content, expected_revision=getattr(args, "expected_revision", None))
        print(f"canonical.status={result.canonical.status.value}\ncanonical.committed=true\nrevision={result.canonical.memory.revision}")
        if result.indexing_error_code:
            print("status=INDEXING_FAILED;canonical_change_saved=true;retry_available=true", file=sys.stderr)
            return 1
        if result.indexing:
            print(f"indexing.status={result.indexing.state.value}")
        print("status=OK")
        return 0
    except (MemoryLifecycleError, MemoryArchivedError) as exc:
        print(f"status=FAIL;code={exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("status=FAIL;code=memory.lifecycle_failed", file=sys.stderr)
        return 1
    finally:
        try:
            if indexer is not None:
                await indexer.aclose()
        finally:
            if engine is not None:
                await engine.dispose()


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if getattr(args, "execute", False) and (not args.confirm_main_memory_write or args.backup_output is None):
        cli.error("Confirmation and backup output are required.")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("status=FAIL;code=interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("status=FAIL;code=memory.lifecycle_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
