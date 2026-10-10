"""Restore a backup into an owned temporary database and verify it."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from kulai_db import DbConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.database_safety import (
    DatabaseSafetyError, OwnedTemporaryDatabase, database_config, database_config_for_database,
    require_owned_database, safe_error_message, validate_restore_source,
)
from kulai_memory.backup_service import RestoreVerificationResult, fingerprint_url, _restore_verified_source
from kulai_memory.settings import get_settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("backup", type=Path)
    result.add_argument("--pre-migration-from", metavar="REVISION")
    return result


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
