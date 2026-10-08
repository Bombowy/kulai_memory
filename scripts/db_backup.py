"""Create an atomic custom-format PostgreSQL backup."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path
from uuid import uuid4

from kulai_db import DbConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.database_safety import (
    DatabaseSafetyError,
    OwnedTemporaryDatabase,
    database_config,
    database_config_for_database,
    database_snapshot_url,
    find_postgres_tool,
    postgres_connection,
    postgres_tool_version,
    require_owned_database,
    run_database_doctor,
    run_postgres_tool,
    safe_error_message,
    validate_backup_doctor,
    validate_backup_output,
    validate_restore_source,
    version_major,
)
from kulai_memory.settings import get_settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--force", action="store_true")
    result.add_argument("--pre-migration-from", metavar="REVISION")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _create_backup(
    output: Path,
    *,
    force: bool,
    config: DbConfig,
    pre_migration_from: str | None = None,
) -> tuple[int, str, str]:
    output = validate_backup_output(output, force=force)

    report = await run_database_doctor(async_url=config.async_url)
    validate_backup_doctor(report, pre_migration_from=pre_migration_from)
    version_check = next(
        check for check in report.checks if check.name == "postgres.version"
    )
    if not isinstance(version_check.value, dict):
        raise DatabaseSafetyError("PostgreSQL server version is unavailable.")
    server_version = str(version_check.value["display"])

    pg_dump = find_postgres_tool("pg_dump")
    pg_dump_version = postgres_tool_version(pg_dump)
    if version_major(pg_dump_version) < version_major(server_version):
        raise DatabaseSafetyError("pg_dump is older than the PostgreSQL server.")

    source_before = await database_snapshot_url(
        config.async_url, pre_migration_from=pre_migration_from,
    )
    connection = postgres_connection(config)
    temporary = output.parent / f".{output.name}.{uuid4().hex}.partial"
    try:
        completed = run_postgres_tool(
            pg_dump,
            [
                *connection.command_arguments(),
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(temporary),
            ],
            connection=connection,
        )
        if completed.returncode != 0:
            raise DatabaseSafetyError(f"pg_dump failed with exit code {completed.returncode}.")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise DatabaseSafetyError("pg_dump did not create a non-empty backup.")
        validate_backup_doctor(
            await run_database_doctor(async_url=config.async_url),
            pre_migration_from=pre_migration_from,
        )
        source_after = await database_snapshot_url(
            config.async_url, pre_migration_from=pre_migration_from,
        )
        if source_after != source_before:
            raise DatabaseSafetyError("Source database changed during backup; archive was not published.")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    return output.stat().st_size, sha256_file(output), pg_dump_version


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
