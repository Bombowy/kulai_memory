"""Read-only by default; guarded indexing of the configured KulAI Memory DB."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
for root in (PROJECT_ROOT, SRC_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from kulai_memory.application import MEMORY_VECTOR_NAMESPACE, MemoryIndexingService  # noqa: E402
from kulai_memory.backfill import (  # noqa: E402
    EMBEDDING_MODEL, VECTOR_DIMENSION, BackfillError, BackfillReader, Progress, execute_batch,
)
from kulai_memory.database_safety import (  # noqa: E402
    database_config, database_config_for_database, database_host_is_loopback,
    require_owned_database, run_database_doctor, validate_restore_source,
)
from kulai_memory.embedding_provider import create_embedding_provider  # noqa: E402
from kulai_memory.settings import get_settings  # noqa: E402
from scripts import db_backup, db_restore_smoke  # noqa: E402
from scripts.embedding_smoke import probe_embedding  # noqa: E402


class _SafeParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        parsed.dry_run = not parsed.execute
        parsed.missing_only = not parsed.reindex
        return parsed

    def error(self, message):
        # argparse's default errors may echo arbitrary invalid argument values.
        super().error("Invalid arguments or missing execute confirmation/backup output.")


def _limit(value: str) -> int:
    try:
        limit = int(value)
        if not 1 <= limit <= 1000:
            raise ValueError
        return limit
    except ValueError:
        raise argparse.ArgumentTypeError("Limit must be between 1 and 1000.") from None


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Memory ID must be a UUID.") from None


def parser() -> argparse.ArgumentParser:
    result = _SafeParser(description=__doc__)
    action = result.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--execute", action="store_true")
    result.add_argument("--limit", type=_limit, default=100)
    result.add_argument("--memory-id", type=_uuid)
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--missing-only", action="store_true")
    selection.add_argument("--reindex", action="store_true")
    result.add_argument("--confirm-main-vector-write", action="store_true")
    result.add_argument("--backup-output", type=Path)
    return result


def validate_backup_path(path: Path | None) -> Path:
    try:
        if path is None or path.expanduser().is_symlink():
            raise ValueError
        resolved = path.expanduser().resolve()
        if resolved.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError
        if resolved.exists() or not resolved.parent.is_dir():
            raise ValueError
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise BackfillError("invalid_backup_path") from None


def _settings_guard(settings, config, *, owned_source):
    if owned_source is None:
        if settings.app_env.casefold() not in {"dev", "development", "local"}:
            raise BackfillError("unsafe_environment")
    if not database_host_is_loopback(config):
        raise BackfillError("non_loopback_database")
    if (
        settings.kulai_vector_dimension != VECTOR_DIMENSION
        or settings.kulai_embedding_model != EMBEDDING_MODEL
    ):
        raise BackfillError("incompatible_configuration")


def _doctor_guard(report):
    dimension = next((check for check in report.checks
                      if check.name == "vector.embedding_dimension"), None)
    if not report.ok:
        raise BackfillError("database_doctor_failed")
    if dimension is None or not isinstance(dimension.value, dict) or (
        dimension.value.get("actual_dimension") != VECTOR_DIMENSION
        or dimension.value.get("configured_dimension") != VECTOR_DIMENSION
    ):
        raise BackfillError("incompatible_schema")


def _print_selection(selection, *, reindex: bool):
    print(f"memory_count={selection.fingerprints[0].count}")
    print(f"existing_vector_count={selection.fingerprints[1].count}")
    print(f"selected_count={len(selection.ids)}")
    print("selected_memory_ids=" + ",".join(str(value) for value in selection.ids))
    print(f"namespace={MEMORY_VECTOR_NAMESPACE}")
    print(f"model={EMBEDDING_MODEL}")
    print(f"dimension={VECTOR_DIMENSION}")
    print("selection=" + ("reindex" if reindex else "missing-only"))
    print(f"incompatible_existing_count={selection.incompatible_existing}")
    if selection.incompatible_existing:
        print("warning=existing_vectors_require_explicit_reindex")


def _print_progress(progress: Progress):
    for field in ("selected", "indexed", "skipped_existing", "failed",
                  "vector_count_before", "vector_count_after",
                  "memory_count_before", "memory_count_after"):
        value = getattr(progress, field)
        print(f"{field}={value if value is not None else 'unavailable'}")
    if progress.failed_id is not None:
        print(f"failed_memory_id={progress.failed_id}")
    unchanged = "unavailable" if progress.memory_unchanged is None else str(progress.memory_unchanged).lower()
    print(f"memory_fingerprint_unchanged={unchanged}")


async def run(args: argparse.Namespace) -> int:
    """Production path: exclusively the configured DB and mandatory write guards."""

    return await _run_target(args, settings=get_settings(), config=database_config())


async def run_owned(args: argparse.Namespace, *, owned, config) -> int:
    """Integration entry point, requiring an existing ownership capability.

    There is no CLI flag or environment bypass exposing this target selection.
    The actual target is derived exclusively from the marker-verified owned DB.
    """

    settings = get_settings()
    validate_restore_source(config, app_env=settings.app_env)
    await require_owned_database(owned, config=config)
    target = database_config_for_database(owned.name, config=config)
    return await _run_target(
        args, settings=settings, config=target, owned_source=owned, source_config=config
    )


async def _run_target(args, *, settings, config, owned_source=None, source_config=None):
    engine = None
    reader = None
    baseline = None
    progress = Progress()
    failure = None
    print("mode=" + ("execute" if args.execute else "dry-run"))
    try:
        if args.execute:
            if owned_source is None and not args.confirm_main_vector_write:
                raise BackfillError("confirmation_required")
            output = validate_backup_path(args.backup_output)
            _settings_guard(settings, config, owned_source=owned_source)
        elif settings.kulai_embedding_model != EMBEDDING_MODEL:
            raise BackfillError("incompatible_configuration")
        _doctor_guard(await run_database_doctor(async_url=config.async_url))
        engine = create_async_engine(config.async_url)
        reader = BackfillReader(async_sessionmaker(engine, expire_on_commit=False))
        baseline = await reader.fingerprints()
        progress.memory_count_before = baseline[0].count
        progress.vector_count_before = baseline[1].count
        if not args.execute:
            selection = await reader.select(
                limit=args.limit, memory_id=args.memory_id, reindex=args.reindex
            )
            _print_selection(selection, reindex=args.reindex)
            progress.selected = len(selection.ids)
            progress.skipped_existing = selection.skipped_existing
        else:
            async with create_embedding_provider(settings=settings) as provider:
                try:
                    probe = await asyncio.wait_for(probe_embedding(
                        provider=provider, expected_dimension=VECTOR_DIMENSION
                    ), timeout=120)
                    if probe.model_id != EMBEDDING_MODEL:
                        raise BackfillError("embedding_preflight_failed")
                except Exception:
                    raise BackfillError("embedding_preflight_failed") from None
                print("embedding_preflight=PASS")
                if await reader.fingerprints() != baseline:
                    raise BackfillError("source_changed")
                try:
                    if owned_source is None:
                        size, digest, _ = await db_backup.create_backup(output, force=False)
                    else:
                        size, digest, _ = await db_backup.create_owned_database_backup(
                            output, force=False, owned=owned_source, config=source_config
                        )
                    if size <= 0 or not output.is_file() or output.stat().st_size != size:
                        raise BackfillError("backup_failed")
                    if db_backup.sha256_file(output) != digest:
                        raise BackfillError("backup_failed")
                except Exception:
                    raise BackfillError("backup_failed") from None
                print(f"backup.size={size}")
                print(f"backup.sha256={digest}")
                try:
                    if owned_source is None:
                        await db_restore_smoke.restore_smoke(output)
                    else:
                        await db_restore_smoke.restore_owned_database_backup(
                            output, owned=owned_source, config=source_config
                        )
                except Exception:
                    raise BackfillError("restore_failed") from None
                if await reader.fingerprints() != baseline:
                    raise BackfillError("source_changed")
                print("restore_smoke=PASS")
                selection = await reader.select(
                    limit=args.limit, memory_id=args.memory_id, reindex=args.reindex
                )
                if selection.fingerprints != baseline:
                    raise BackfillError("source_changed")
                _print_selection(selection, reindex=args.reindex)
                service = MemoryIndexingService(provider=provider, expected_dimension=VECTOR_DIMENSION)
                await execute_batch(
                    reader=reader, service=service, selection=selection,
                    reindex=args.reindex, progress=progress,
                )
    except BackfillError as exc:
        failure = exc.code
    except Exception:
        failure = "operation_failed"
    finally:
        if reader is not None and baseline is not None:
            try:
                after = await reader.fingerprints()
                progress.memory_count_after, progress.vector_count_after = after[0].count, after[1].count
                progress.memory_unchanged = after[0] == baseline[0]
                if not progress.memory_unchanged:
                    failure = failure or "memory_changed"
                    print("warning=memory_changed")
                if not args.execute and after != baseline:
                    failure = failure or "source_changed"
            except Exception:
                failure = failure or "verification_failed"
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                failure = failure or "cleanup_failed"
        _print_progress(progress)
    if failure:
        print(f"status=FAIL;code={failure}", file=sys.stderr)
        return 1
    print("status=" + ("OK" if args.execute else "DRY_RUN_OK"))
    return 0


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.execute and (not args.confirm_main_vector_write or args.backup_output is None):
        cli.error("Missing confirmation or backup output.")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("status=FAIL;code=interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("status=FAIL;code=operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
