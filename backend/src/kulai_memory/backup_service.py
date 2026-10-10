"""Shared host-only PostgreSQL backup and restore verification operations."""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from kulai_db import DbConfig
from kulai_memory.database_safety import (
    DatabaseSafetyError,
    DatabaseSnapshot,
    OwnedTemporaryDatabase,
    async_database_url,
    create_owned_temporary_database,
    database_config_for_database,
    database_snapshot_url,
    drop_owned_temporary_database,
    find_postgres_tool,
    postgres_connection,
    postgres_tool_version,
    require_owned_database,
    restore_archive_to_owned_database,
    run_database_doctor,
    run_postgres_tool,
    validate_backup_doctor,
    validate_backup_output,
    validate_restore_source,
    version_major,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedBackupResult:
    """Retained archive identity and complete hashes/counts, without row contents."""
    path: Path
    size_bytes: int
    sha256: str
    source_snapshot: DatabaseSnapshot


@dataclass(frozen=True, slots=True)
class _BackupArchiveResult:
    backup: VerifiedBackupResult
    pg_dump_version: str


def validate_delete_backup_output(output: Path) -> Path:
    if not isinstance(output, Path) or output.suffix.lower() != ".dump":
        raise ValueError("Choose a new .dump backup outside the repository.")
    return validate_backup_output(output, force=False)


async def drain_before_cancellation(task: asyncio.Task):
    """Finish owned resource work, then propagate cancellation; never proceed to delete."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # consume any failure while retaining the cancellation outcome
        raise


def _revalidate_backup_archive(backup: VerifiedBackupResult) -> None:
    # This validation only reads an existing retained file; force never writes here.
    path = validate_backup_output(backup.path, force=True)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size != backup.size_bytes:
        raise DatabaseSafetyError("Verified backup archive does not match its token.")
    digest = sha256_file(path)
    after = path.stat(follow_symlinks=False)
    validate_backup_output(path, force=True)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if not stat.S_ISREG(after.st_mode) or identity(before) != identity(after) or digest != backup.sha256:
        raise DatabaseSafetyError("Verified backup archive does not match its token.")


async def revalidate_verified_backup(backup: VerifiedBackupResult, *, config: DbConfig) -> None:
    """Recheck the full source and retained archive token just before destructive work.

    No backup is replaced or removed. Cancellation propagates after any file reader
    has drained, and never grants permission to start the delete transaction.
    """
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError
    try:
        if await database_snapshot_url(config.async_url) != backup.source_snapshot:
            raise DatabaseSafetyError("Verified backup source does not match its token.")
        await drain_before_cancellation(asyncio.create_task(asyncio.to_thread(_revalidate_backup_archive, backup)))
    except Exception:
        raise DatabaseSafetyError("Verified backup revalidation could not be completed.") from None
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError


async def _create_backup(
    output: Path, *, force: bool, config: DbConfig, pre_migration_from: str | None = None,
) -> tuple[int, str, str]:
    result = await drain_before_cancellation(asyncio.create_task(_backup_archive(
        output, force=force, config=config, pre_migration_from=pre_migration_from)))
    return result.backup.size_bytes, result.backup.sha256, result.pg_dump_version


async def _backup_archive(
    output: Path,
    *,
    force: bool,
    config: DbConfig,
    pre_migration_from: str | None = None,
    retain_on_failure: bool = False,
) -> _BackupArchiveResult:
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
    pg_dump_version = await asyncio.to_thread(postgres_tool_version, pg_dump)
    if version_major(pg_dump_version) < version_major(server_version):
        raise DatabaseSafetyError("pg_dump is older than the PostgreSQL server.")

    source_before = await database_snapshot_url(
        config.async_url, pre_migration_from=pre_migration_from,
    )
    connection = postgres_connection(config)
    temporary = output.parent / f".{output.name}.{uuid4().hex}.partial"
    if retain_on_failure:
        # O_EXCL also closes the validation -> creation overwrite race.
        with output.open("xb"):
            pass
        temporary = output
    try:
        completed = await asyncio.to_thread(run_postgres_tool,
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
        if not retain_on_failure:
            if force:
                os.replace(temporary, output)
            else:
                os.link(temporary, output)  # publish atomically without replacing a raced-in file
    finally:
        if not retain_on_failure and temporary.exists():
            temporary.unlink()

    return _BackupArchiveResult(VerifiedBackupResult(output, output.stat().st_size,
        await asyncio.to_thread(sha256_file, output), source_before), pg_dump_version)


@dataclass(frozen=True, slots=True)
class RestoreVerificationResult:
    database: str
    snapshot: DatabaseSnapshot


async def fingerprint_url(
    url: str, *, pre_migration_from: str | None = None,
) -> DatabaseSnapshot:
    return await database_snapshot_url(url, pre_migration_from=pre_migration_from)


async def _restore_verified_source(
    backup: Path, *, config: DbConfig, pre_migration_from: str | None = None,
) -> RestoreVerificationResult:
    return await drain_before_cancellation(asyncio.create_task(_restore_source(
        backup, config=config, pre_migration_from=pre_migration_from)))


async def _restore_source(
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


async def create_verified_backup(
    output: Path, *, config: DbConfig, app_env: str,
    on_verifying: Callable[[], None] | None = None,
    owned: OwnedTemporaryDatabase | None = None,
) -> VerifiedBackupResult:
    """Strict, retained backup plus actual restore verification before a Desktop delete.

    An owned-source capability is for synthetic integrations only. Public Desktop
    uses its configured local development source; there is no target override UI.
    Cancellation drains administrative work and never returns a delete permission.
    """
    validate_restore_source(config, app_env=app_env)
    output = validate_delete_backup_output(output)
    if owned is not None:
        await require_owned_database(owned, config=config)
        config = database_config_for_database(owned.name, config=config)

    async def verify() -> VerifiedBackupResult:
        archive = (await _backup_archive(output, force=False, config=config, retain_on_failure=True)).backup
        if archive.source_snapshot.revision != ("kulai_memory_0004",):
            raise DatabaseSafetyError("Desktop deletion requires schema kulai_memory_0004.")
        if on_verifying is not None:
            on_verifying()
        restored = await _restore_verified_source(output, config=config)
        if restored.snapshot != archive.source_snapshot:
            raise DatabaseSafetyError("Source changed between backup and restore verification.")
        if await database_snapshot_url(config.async_url) != archive.source_snapshot:
            raise DatabaseSafetyError("Source changed after backup verification.")
        if output.stat().st_size != archive.size_bytes or await asyncio.to_thread(sha256_file, output) != archive.sha256:
            raise DatabaseSafetyError("Backup archive changed during verification.")
        return archive

    return await drain_before_cancellation(asyncio.create_task(verify()))
