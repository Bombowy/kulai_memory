"""Explicit test-only installation. No main installation function or startup hook."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ..database_safety import database_config_for_database, postgres_connection, require_owned_database, run_database_doctor
from ..application.fact_memory import FactMemoryError
from .schema_v2 import FACT_HEAD

INI = Path(__file__).resolve().parents[3] / "facts_alembic.ini"


def fact_migration_heads():
    return tuple(ScriptDirectory.from_config(Config(str(INI))).get_heads())


async def upgrade_owned_facts(owned, *, config, revision=FACT_HEAD):
    try:
        if revision not in {"kulai_facts_0001", FACT_HEAD}:
            raise FactMemoryError("invalid_input")
        await require_owned_database(owned, config=config)
        target = database_config_for_database(owned.name, config=config)
        doctor = await run_database_doctor(async_url=target.async_url)
        if not doctor.ok:
            raise FactMemoryError("schema_required")
        engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='5s'"))
                await connection.execute(text("SELECT pg_advisory_xact_lock(49145002)"))
                def upgrade(sync_connection):
                    active = Config(str(INI))
                    active.attributes.update(connection=sync_connection, owned=owned, source_database=postgres_connection(config).database)
                    command.upgrade(active, revision)
                await connection.run_sync(upgrade)
        finally:
            await engine.dispose()
    except FactMemoryError:
        raise
    except Exception:
        raise FactMemoryError("owned_database_required") from None
