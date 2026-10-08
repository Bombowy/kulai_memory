"""Restore a backup into an owned temporary database and verify it."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from kulai_db import DbConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.database_safety import (
    DatabaseSafetyError,
    DatabaseSnapshot,
    OwnedTemporaryDatabase,
    async_database_url,
    create_owned_temporary_database,
    database_config,
    database_config_for_database,
    database_snapshot_url,
    drop_owned_temporary_database,
    require_owned_database,
    restore_archive_to_owned_database,
    run_database_doctor,
    safe_error_message,
    validate_backup_doctor,
    validate_restore_source,
)
from kulai_memory.settings import get_settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("backup", type=Path)
    result.add_argument("--pre-migration-from", metavar="REVISION")
    return result


@dataclass(frozen=True, slots=True)
class RestoreVerificationResult:
    database: str
    snapshot: DatabaseSnapshot


async def fingerprint_url(
    url: str, *, pre_migration_from: str | None = None,
) -> DatabaseSnapshot:
    return await database_snapshot_url(url, pre_migration_from=pre_migration_from)


async def restore_smoke(
    backup: Path, *, pre_migration_from: str | None = None,
) -> RestoreVerificationResult:
    config = database_config()
    validate_restore_source(config, app_env=get_settings().app_env)
    return await _restore_verified_source(
        backup, config=config, pre_migration_from=pre_migration_from,
    )


async def restore_owned_database_backup(
    backup: Path, *, owned: OwnedTemporaryDatabase, config: DbConfig,
    pre_migration_from: str | None = None,
) -> RestoreVerificationResult:
    """Restore only a marker-verified owned source; no CLI target override."""

    validate_restore_source(config, app_env=get_settings().app_env)
    await require_owned_database(owned, config=config)
    target = database_config_for_database(owned.name, config=config)
    return await _restore_verified_source(
        backup, config=target, pre_migration_from=pre_migration_from,
    )


async def _restore_verified_source(
    backup: Path, *, config: DbConfig, pre_migration_from: str | None = None,
) -> RestoreVerificationResult:
    archive = backup.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError("Backup file does not exist.")

    validate_backup_doctor(
        await run_database_doctor(async_url=config.async_url),
        pre_migration_from=pre_migration_from,
    )

    source_before = await fingerprint_url(config.async_url, pre_migration_from=pre_migration_from)
    owned: OwnedTemporaryDatabase | None = None
    verified = False
    cleanup_error: Exception | None = None
    try:
        owned = await create_owned_temporary_database(kind="restore", config=config)
        await restore_archive_to_owned_database(archive, owned, config=config)

        restored_url = async_database_url(database=owned.name, config=config)
        doctor = await run_database_doctor(async_url=restored_url)
        validate_backup_doctor(doctor, pre_migration_from=pre_migration_from)
        restored = await fingerprint_url(restored_url, pre_migration_from=pre_migration_from)
        source_after = await fingerprint_url(config.async_url, pre_migration_from=pre_migration_from)
        validate_backup_doctor(
            await run_database_doctor(async_url=config.async_url),
            pre_migration_from=pre_migration_from,
        )
        if source_before != source_after:
            raise DatabaseSafetyError("Source database changed during restore verification.")
        if restored != source_before:
            raise DatabaseSafetyError("Restored database fingerprints do not match source.")
        verified = True
        return RestoreVerificationResult(database=owned.name, snapshot=restored)
    finally:
        if owned is not None:
            try:
                await drop_owned_temporary_database(owned, config=config)
            except Exception as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            action = "after verification" if verified else "after failure"
            raise DatabaseSafetyError(
                f"Safe cleanup failed {action} for {owned.name}: "
                f"{type(cleanup_error).__name__}."
            ) from cleanup_error


async def run(backup: Path, *, pre_migration_from: str | None = None) -> int:
    result = await restore_smoke(
        backup, pre_migration_from=pre_migration_from,
    )
    print(f"Mode: {'pre-migration' if pre_migration_from else 'strict'}")
    if pre_migration_from:
        print(f"Source/restored revision verified: {pre_migration_from}")
    print(f"Restore verified in owned temporary database: {result.database}")
    print(f"Memory rows verified: {result.snapshot.memories.count}")
    print(f"Vector rows verified: {result.snapshot.vectors.count}")
    tombstones = result.snapshot.tombstones
    if tombstones is None:
        print("Tombstones verified: absent (pre-migration 0002)")
    else:
        print(f"Tombstone rows verified: {tombstones.count}")
        print(f"Tombstone SHA-256: {tombstones.sha256}")
    print("Owned temporary database removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args.backup, pre_migration_from=args.pre_migration_from))
    except Exception as exc:
        if isinstance(exc, DatabaseSafetyError):
            message = str(exc)
        elif isinstance(exc, FileNotFoundError):
            message = "Required backup file or PostgreSQL client tool is unavailable."
        elif isinstance(exc, ValueError):
            message = "Invalid backup or database safety configuration."
        else:
            message = safe_error_message(exc, operation="Restore smoke")
        print(f"Restore smoke failed safely: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
