"""Composition-owned security pool. No providers, migrations or credential bootstrap."""
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from kulai_db import DbConfig
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy import text

from kulai_memory.database_safety import DoctorReport, database_config, run_database_doctor
from kulai_memory.backup_service import drain_before_cancellation
from kulai_memory.security.models import CredentialState, SecurityError
from kulai_memory.security.storage import SecurityReader
from .postgres import PostgresSecurityStore
from .pairing import PostgresPairingStore
from .integrity import inspect_security_schema
from .schema_v2 import SECURITY_HEAD
from .schema_v3 import SECURITY_HEAD as SESSION_HEAD
from .sessions import PostgresSessionStore
from kulai_memory.security.sessions import SessionService
from kulai_memory.pairing.domain import PairingService
from kulai_memory.security.authentication import utc_now

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
        self.pairing_service: PairingService | None = None
        self.session_service: SessionService | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._remote_closed = False
        self._validated_revision = None
        self._recovery_gate = None
        self._ingress_lease = None

    def configure_recovery_gate(self, directory):
        if self.ready or self._engine is not None:
            raise SecurityError()
        from kulai_memory.security_recovery import RecoveryGate
        self._recovery_gate = RecoveryGate(directory)

    async def startup(self) -> None:
        async with self._lifecycle_lock:
            await self._startup()

    async def _startup(self) -> None:
        if self.ready:
            return
        try:
            config = self._config_factory()
            if self._recovery_gate is None and os.environ.get('KULAI_RECOVERY_GATE'):
                from kulai_memory.security_recovery import RecoveryGate
                self._recovery_gate = RecoveryGate(os.environ['KULAI_RECOVERY_GATE'])
            report = await self._doctor(async_url=config.async_url, require_security=True)
            if not report.ok:
                raise SecurityError()
            self._engine = self._engine_factory(config.async_url, echo=False, hide_parameters=True,
                pool_size=config.pool_size, max_overflow=config.max_overflow, pool_pre_ping=True,
                connect_args={"server_settings": {"statement_timeout": "4000", "lock_timeout": "3000", "search_path": "pg_catalog,public"}})
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            self._store = PostgresSecurityStore(factory)
            async with self._engine.connect() as connection:
                await connection.execute(text('SET TRANSACTION READ ONLY'))
                state = await inspect_security_schema(connection)
                await connection.rollback()
            if not state.valid or not state.present:
                raise SecurityError()
            self._validated_revision = state.revision
            if self._recovery_gate is not None:
                from kulai_memory.security_recovery.gate import FileLease
                self._ingress_lease = FileLease(self._recovery_gate.ingress, exclusive=False)
                if not self._ingress_lease.try_lock():
                    raise SecurityError()
                await self._recovery_gate.check(factory)
            if state.revision == SESSION_HEAD:
                self._store = PostgresSessionStore(factory, recovery_gate=self._recovery_gate)
                self.pairing_service = PairingService(self._store, clock=utc_now)
                self.session_service = SessionService(self._store)
            elif state.revision == SECURITY_HEAD:
                self.pairing_service = PairingService(PostgresPairingStore(factory), clock=utc_now)
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
        try:
            result = await store.lookup(digest)
        except Exception:
            self._remote_closed = True
            raise SecurityError() from None
        if not self.ready:
            raise SecurityError()
        return result

    @property
    def remote_ready(self) -> bool:
        return (self.ready and not self._remote_closed and self._validated_revision == SESSION_HEAD
                and isinstance(self._store, PostgresSessionStore) and self.session_service is not None
                and self._recovery_gate is not None and self._ingress_lease is not None)

    async def require_remote_ready(self) -> None:
        """Actual read-only catalog validation, never a sidecar environment claim."""
        try:
            if not self.remote_ready or self._engine is None:
                raise SecurityError()
            async with asyncio.timeout(5):
                await self._recovery_gate.check(async_sessionmaker(self._engine, expire_on_commit=False))
                async with self._engine.connect() as connection:
                    try:
                        await connection.execute(text('SET TRANSACTION READ ONLY'))
                        heads = tuple((await connection.execute(text(
                            'SELECT version_num FROM public.alembic_version ORDER BY version_num'))).scalars())
                        state = await inspect_security_schema(connection)
                        if heads not in (('kulai_memory_0004',), ('kulai_memory_0005',)) or not state.valid or state.revision != SESSION_HEAD:
                            raise SecurityError()
                    finally:
                        await connection.rollback()
        except Exception:
            self._remote_closed = True  # Restart/manual launcher required after failure.
            raise SecurityError() from None

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self) -> None:
        self.ready = False
        self._validated_revision = None
        self.pairing_service = None
        self.session_service = None
        self._store = None
        engine, self._engine = self._engine, None
        try:
            if engine is not None:
                await drain_before_cancellation(asyncio.create_task(engine.dispose()))
        except Exception:
            raise SecurityError() from None
        finally:
            if self._ingress_lease is not None:
                self._ingress_lease.close()
                self._ingress_lease = None
