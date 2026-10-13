"""Owned synthetic DB only, with two independent Alembic graphs."""
import asyncio
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.integration.test_memory_postgres import _upgrade_database, _require_opt_in
from kulai_memory.database_safety import (
    database_config, database_config_for_database, create_owned_temporary_database,
    drop_owned_temporary_database, require_owned_database,
)
from kulai_memory.security_persistence.migration import upgrade_owned_security
from kulai_memory.security_persistence.postgres import PostgresSecurityStore


@asynccontextmanager
async def owned_store(*, security=True):
    _require_opt_in()
    config = database_config()
    owned = await create_owned_temporary_database(kind="backup", config=config)
    engine = None
    try:
        await require_owned_database(owned, config=config)
        target = database_config_for_database(owned.name, config=config)
        await asyncio.to_thread(_upgrade_database, target.async_url, "head")
        if security:
            await upgrade_owned_security(owned, config=config)
        engine = create_async_engine(target.async_url, echo=False, hide_parameters=True, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield owned, config, target, factory, PostgresSecurityStore(factory)
    finally:
        if engine is not None:
            await engine.dispose()
        await drop_owned_temporary_database(owned, config=config)
