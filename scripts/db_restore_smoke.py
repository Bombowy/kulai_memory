"""Restore a backup into an owned temporary database and verify it."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.database_safety import (
    MemoryFingerprint,
    OwnedTemporaryDatabase,
    VectorFingerprint,
    async_database_url,
    create_owned_temporary_database,
    database_config,
    drop_owned_temporary_database,
    memory_fingerprint,
    restore_archive_to_owned_database,
    run_database_doctor,
    validate_restore_source,
    vector_fingerprint,
)
from kulai_memory.settings import get_settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("backup", type=Path)
    return result


async def fingerprint_url(
    url: str,
) -> tuple[MemoryFingerprint, VectorFingerprint]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            memories = await memory_fingerprint(connection)
            vectors = await vector_fingerprint(connection)
            await connection.rollback()
            return memories, vectors
    finally:
        await engine.dispose()


async def restore_smoke(backup: Path) -> tuple[str, int, int]:
    archive = backup.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError("Backup file does not exist.")

    settings = get_settings()
    config = database_config()
    validate_restore_source(config, app_env=settings.app_env)
    source_before = await fingerprint_url(config.async_url)
    owned: OwnedTemporaryDatabase | None = None
    verified = False
    cleanup_error: Exception | None = None
    try:
        owned = await create_owned_temporary_database(kind="restore", config=config)
        await restore_archive_to_owned_database(archive, owned, config=config)

        restored_url = async_database_url(database=owned.name, config=config)
        doctor = await run_database_doctor(async_url=restored_url)
        if not doctor.ok:
            raise RuntimeError("Restored database invariants failed.")
        restored = await fingerprint_url(restored_url)
        source_after = await fingerprint_url(config.async_url)
        if source_before != source_after:
            raise RuntimeError("Source database changed during restore verification.")
        if restored != source_before:
            raise RuntimeError("Restored database fingerprints do not match source.")
        verified = True
        return owned.name, restored[0].count, restored[1].count
    finally:
        if owned is not None:
            try:
                await drop_owned_temporary_database(owned, config=config)
            except Exception as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            action = "after verification" if verified else "after failure"
            raise RuntimeError(
                f"Safe cleanup failed {action} for {owned.name}: "
                f"{type(cleanup_error).__name__}."
            ) from cleanup_error


async def run(backup: Path) -> int:
    database, memories, vectors = await restore_smoke(backup)
    print(f"Restore verified in owned temporary database: {database}")
    print(f"Memory rows verified: {memories}")
    print(f"Vector rows verified: {vectors}")
    print("Owned temporary database removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args.backup))
    except Exception as exc:
        if isinstance(exc, (FileNotFoundError, RuntimeError)):
            message = str(exc)
        elif isinstance(exc, ValueError):
            message = "Invalid backup or database safety configuration."
        else:
            message = f"Unexpected {type(exc).__name__}."
        print(f"Restore smoke failed safely: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
