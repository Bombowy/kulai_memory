"""Guarded additive security upgrade: independent graph, explicit connection only."""
from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from kulai_db import DbConfig

from kulai_memory.database_safety import (
    DatabaseSafetyError, OwnedTemporaryDatabase, database_config_for_database, require_owned_database,
    run_database_doctor, memory_fingerprint, vector_fingerprint, tombstone_fingerprint,
)
from kulai_memory.backup_service import VerifiedBackupResult, create_verified_backup, revalidate_verified_backup
from .integrity import inspect_security_schema
from .schema_v3 import SECURITY_HEAD

INI = Path(__file__).resolve().parents[3] / "security_alembic.ini"


def security_migration_heads() -> tuple[str, ...]:
    return tuple(ScriptDirectory.from_config(Config(str(INI))).get_heads())


def _upgrade(connection, revision: str = SECURITY_HEAD) -> None:
    config = Config(str(INI))
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


async def upgrade_owned_security(owned: OwnedTemporaryDatabase, *, config: DbConfig) -> None:
    """Synthetic test capability; no direct main URL override or CLI shortcut."""
    await require_owned_database(owned, config=config)
    target = database_config_for_database(owned.name, config=config)
    if not (await run_database_doctor(async_url=target.async_url)).ok:
        raise DatabaseSafetyError("Strict canonical doctor is required before security upgrade.")
    engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL search_path TO pg_catalog, public"))
            await connection.run_sync(_upgrade)
    except Exception:
        raise DatabaseSafetyError("Security upgrade failed safely.") from None
    finally:
        await engine.dispose()


async def upgrade_security_with_verified_backup(output: Path, *, config: DbConfig, app_env: str,
                                                owned: OwnedTemporaryDatabase | None = None) -> VerifiedBackupResult:
    """Trusted administration only; no automatic migration in application startup."""
    if owned is not None:
        await require_owned_database(owned, config=config)
    backup = await create_verified_backup(output, config=config, app_env=app_env, owned=owned)
    target = database_config_for_database(owned.name, config=config) if owned else config
    previous = backup.source_snapshot.security
    if previous is not None and previous.revision == (SECURITY_HEAD,):
        raise DatabaseSafetyError("Security schema is already installed.")
    await revalidate_verified_backup(backup, config=target)
    engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL search_path TO pg_catalog, public"))
            await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            await connection.execute(text("SELECT pg_advisory_xact_lock(49142001)"))
            state = await inspect_security_schema(connection)
            if not state.valid or state.revision != (previous.revision[0] if previous else None):
                raise DatabaseSafetyError("Security schema changed before upgrade.")
            async def unchanged():
                return (await memory_fingerprint(connection) == backup.source_snapshot.memories
                    and await vector_fingerprint(connection) == backup.source_snapshot.vectors
                    and await tombstone_fingerprint(connection) == backup.source_snapshot.tombstones)
            if not await unchanged():
                raise DatabaseSafetyError("Source changed before security upgrade.")
            if previous is not None:
                from .integrity import security_snapshot
                if await security_snapshot(connection) != previous:
                    raise DatabaseSafetyError('Security state changed before upgrade.')
            await connection.run_sync(_upgrade)
            if not await unchanged():
                raise DatabaseSafetyError("Canonical state changed during security upgrade.")
            if previous is not None:
                updated = await security_snapshot(connection)
                if (updated.devices != previous.devices or updated.tokens != previous.tokens
                        or previous.pairings is not None and updated.pairings != previous.pairings
                        or previous.pairing_rate is not None and updated.pairing_rate != previous.pairing_rate):
                    raise DatabaseSafetyError('Existing security state changed during upgrade.')
            state = await inspect_security_schema(connection)
            if not state.present or not state.valid:
                raise DatabaseSafetyError("Security schema verification failed.")
        if not (await run_database_doctor(async_url=target.async_url, require_security=True)).ok:
            raise DatabaseSafetyError("Post-upgrade strict doctor failed.")
        return backup
    except Exception:
        raise DatabaseSafetyError("Security upgrade failed safely; retain the verified backup.") from None
    finally:
        await engine.dispose()
