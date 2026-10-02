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
    OwnedTemporaryDatabase,
    database_config,
    database_config_for_database,
    find_postgres_tool,
    postgres_connection,
    postgres_tool_version,
    require_owned_database,
    run_database_doctor,
    run_postgres_tool,
    version_major,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--force", action="store_true")
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
) -> tuple[int, str, str]:
    output = output.expanduser()
    if output.is_symlink():
        raise ValueError("Backup output must not be a symbolic link.")
    output = Path(os.path.abspath(output))
    if output.exists() and not force:
        raise FileExistsError("Output already exists; pass --force to replace it.")
    if output.exists() and not output.is_file():
        raise ValueError("Backup output must be a regular file path.")
    if not output.parent.is_dir():
        raise FileNotFoundError("Backup output directory does not exist.")

    report = await run_database_doctor(async_url=config.async_url)
    if not report.ok:
        raise RuntimeError("Database invariants failed; backup was not started.")
    version_check = next(
        check for check in report.checks if check.name == "postgres.version"
    )
    if not isinstance(version_check.value, dict):
        raise RuntimeError("PostgreSQL server version is unavailable.")
    server_version = str(version_check.value["display"])

    pg_dump = find_postgres_tool("pg_dump")
    pg_dump_version = postgres_tool_version(pg_dump)
    if version_major(pg_dump_version) < version_major(server_version):
        raise RuntimeError("pg_dump is older than the PostgreSQL server.")

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
            raise RuntimeError(f"pg_dump failed with exit code {completed.returncode}.")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("pg_dump did not create a non-empty backup.")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    return output.stat().st_size, sha256_file(output), pg_dump_version


async def create_backup(output: Path, *, force: bool) -> tuple[int, str, str]:
    """Back up only the database selected by the host's active settings."""

    return await _create_backup(output, force=force, config=database_config())


async def create_owned_database_backup(
    output: Path,
    *,
    force: bool,
    owned: OwnedTemporaryDatabase,
    config: DbConfig,
) -> tuple[int, str, str]:
    """Internal backup API restricted to a marker-verified temporary database."""

    await require_owned_database(owned, config=config)
    target_config = database_config_for_database(owned.name, config=config)
    return await _create_backup(output, force=force, config=target_config)


async def run(args: argparse.Namespace) -> int:
    size, digest, version = await create_backup(args.output, force=args.force)
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
        if isinstance(exc, (FileExistsError, FileNotFoundError, RuntimeError)):
            message = str(exc)
        elif isinstance(exc, ValueError):
            message = "Invalid backup path or database configuration."
        else:
            message = f"Unexpected {type(exc).__name__}."
        print(f"Backup failed safely: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
