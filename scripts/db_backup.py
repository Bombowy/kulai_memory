"""Create an atomic custom-format PostgreSQL backup."""

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
from kulai_memory.backup_service import sha256_file, _create_backup
from kulai_memory.settings import get_settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--force", action="store_true")
    result.add_argument("--pre-migration-from", metavar="REVISION")
    return result


async def create_backup(
    output: Path, *, force: bool, pre_migration_from: str | None = None,
) -> tuple[int, str, str]:
    """Back up only the database selected by the host's active settings."""

    config = database_config()
    validate_restore_source(config, app_env=get_settings().app_env)
    return await _create_backup(
        output, force=force, config=config, pre_migration_from=pre_migration_from,
    )


async def create_owned_database_backup(
    output: Path,
    *,
    force: bool,
    owned: OwnedTemporaryDatabase,
    config: DbConfig,
    pre_migration_from: str | None = None,
) -> tuple[int, str, str]:
    """Internal backup API restricted to a marker-verified temporary database."""

    validate_restore_source(config, app_env=get_settings().app_env)
    await require_owned_database(owned, config=config)
    target_config = database_config_for_database(owned.name, config=config)
    return await _create_backup(
        output, force=force, config=target_config, pre_migration_from=pre_migration_from,
    )


async def run(args: argparse.Namespace) -> int:
    size, digest, version = await create_backup(
        args.output, force=args.force, pre_migration_from=args.pre_migration_from,
    )
    print(f"Mode: {'pre-migration' if args.pre_migration_from else 'strict'}")
    if args.pre_migration_from:
        print(f"Source revision: {args.pre_migration_from}")
    print(f"Backup: {args.output.expanduser().resolve()}")
    print(f"Size: {size} bytes")
    print(f"SHA-256: {digest}")
    print(f"pg_dump: {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        if isinstance(exc, DatabaseSafetyError):
            message = str(exc)
        elif isinstance(exc, FileExistsError):
            message = "Output already exists; pass --force to replace it."
        elif isinstance(exc, FileNotFoundError):
            message = "Required directory or PostgreSQL client tool is unavailable."
        elif isinstance(exc, ValueError):
            message = "Invalid backup path or database configuration."
        else:
            message = safe_error_message(exc, operation="Backup")
        print(f"Backup failed safely: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
