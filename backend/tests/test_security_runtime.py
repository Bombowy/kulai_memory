"""Composition lifecycle and fail-closed errors without contacting any database."""
import asyncio
import logging

import pytest
from kulai_db import DbConfig

from backend.tests.local_transport import TestClient, create_local_app
from backend.tests.test_security_transport import Runtime
from kulai_memory.app_factory import create_app
from kulai_memory.database_safety import DoctorReport, CheckResult
from kulai_memory.security import SecurityError
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime, ReaderBinding


def test_missing_schema_prevents_engine_and_ai_startup(caplog):
    async def doctor(**kwargs):
        assert kwargs["require_security"] is True
        return DoctorReport((CheckResult("security.schema", False),))
    def forbidden_engine(*args, **kwargs):
        pytest.fail("Invalid schema opened an authentication pool.")
    runtime = Runtime()
    owner = PostgresSecurityRuntime(config_factory=lambda: DbConfig(), doctor=doctor, engine_factory=forbidden_engine)
    app = create_app(voice_runtime_factory=lambda: runtime, security_runtime_factory=lambda: owner)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/memories", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
    assert not owner.ready and runtime.startup_count == runtime.shutdown_count == 0


def test_local_dev_does_not_construct_security_pool():
    runtime = Runtime()
    def forbidden_owner():
        pytest.fail("LOCAL_DEV opened persistent security storage.")
    app = create_local_app(voice_runtime_factory=lambda: runtime, security_runtime_factory=forbidden_owner)
    with TestClient(app) as client:
        assert client.get("/api/memories").status_code == 200
    assert runtime.startup_count == runtime.shutdown_count == 1


def test_storage_diagnostics_do_not_echo_database_or_token_errors(caplog):
    async def scenario():
        async def doctor(**kwargs):
            raise RuntimeError("PRIVATE_CREDENTIAL_SENTINEL")
        logger = logging.getLogger("kulai_memory.security_persistence.runtime")
        logger.addHandler(caplog.handler)
        try:
            owner = PostgresSecurityRuntime(config_factory=lambda: DbConfig(), doctor=doctor)
            await owner.startup()
            assert not owner.ready
            with pytest.raises(SecurityError):
                await owner.lookup("x" * 64)
            await owner.shutdown()
        finally:
            logger.removeHandler(caplog.handler)
        assert "PRIVATE_CREDENTIAL_SENTINEL" not in caplog.text
    asyncio.run(scenario())


def test_shutdown_cancellation_drains_dispose_once_and_binding_refuses_late_reads():
    async def scenario():
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Engine:
            calls = 0
            async def dispose(self):
                self.calls += 1
                entered.set()
                await release.wait()
                closed.set()
        owner = PostgresSecurityRuntime()
        engine = owner._engine = Engine()
        binding = ReaderBinding()
        binding.reader = owner
        task = asyncio.create_task(owner.shutdown())
        await entered.wait(); task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owner.shutdown()
        assert closed.is_set() and engine.calls == 1
        with pytest.raises(SecurityError):
            await binding.lookup("x" * 64)
    asyncio.run(scenario())


def test_dispose_error_is_sanitized_and_runtime_remains_unavailable():
    async def scenario():
        class Engine:
            async def dispose(self):
                raise RuntimeError("PRIVATE_DISPOSE_PASSWORD")
        owner = PostgresSecurityRuntime()
        owner._engine = Engine()
        with pytest.raises(SecurityError) as caught:
            await owner.shutdown()
        assert caught.value.__cause__ is None
        assert str(caught.value) == "Authentication required."
        assert not owner.ready
    asyncio.run(scenario())
