"""Composition-owned security pool. No providers, migrations or credential bootstrap."""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from kulai_db import DbConfig
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from kulai_memory.database_safety import DoctorReport, database_config, run_database_doctor
from kulai_memory.backup_service import drain_before_cancellation
from kulai_memory.security.models import CredentialState, SecurityError
from kulai_memory.security.storage import SecurityReader
from .postgres import PostgresSecurityStore

logger = logging.getLogger(__name__)


class ReaderBinding:
    """Middleware sees only the currently active lifespan's verified reader."""

    def __init__(self) -> None:
        self.reader: SecurityReader | None = None

    async def lookup(self, digest: str) -> CredentialState | None:
        reader = self.reader
        if reader is None:
            raise SecurityError()
        return await reader.lookup(digest)


class PostgresSecurityRuntime:
    def __init__(self, *, config_factory: Callable[[], DbConfig] = database_config,
                 doctor: Callable[..., Awaitable[DoctorReport]] = run_database_doctor,
                 engine_factory: Callable[..., AsyncEngine] = create_async_engine) -> None:
        self._config_factory, self._doctor, self._engine_factory = config_factory, doctor, engine_factory
        self._engine: AsyncEngine | None = None
        self._store: PostgresSecurityStore | None = None
        self.ready = False
        self._lifecycle_lock = asyncio.Lock()

    async def startup(self) -> None:
        async with self._lifecycle_lock:
            await self._startup()

    async def _startup(self) -> None:
        if self.ready:
            return
        try:
            config = self._config_factory()
            report = await self._doctor(async_url=config.async_url, require_security=True)
            if not report.ok:
                raise SecurityError()
            self._engine = self._engine_factory(config.async_url, echo=False, hide_parameters=True,
                pool_size=config.pool_size, max_overflow=config.max_overflow, pool_pre_ping=True,
                connect_args={"server_settings": {"statement_timeout": "4000", "lock_timeout": "3000", "search_path": "pg_catalog,public"}})
            self._store = PostgresSecurityStore(async_sessionmaker(self._engine, expire_on_commit=False))
            self.ready = True
        except asyncio.CancelledError:
            await self._close()
            raise
        except Exception:
            await self._close()
            logger.warning("security storage unavailable stage=schema_validation")

    async def lookup(self, digest: str) -> CredentialState | None:
        store = self._store
        if not self.ready or store is None:
            raise SecurityError()
        result = await store.lookup(digest)
        if not self.ready:
            raise SecurityError()
        return result

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self) -> None:
        self.ready = False
        self._store = None
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                await drain_before_cancellation(asyncio.create_task(engine.dispose()))
            except Exception:
                raise SecurityError() from None
