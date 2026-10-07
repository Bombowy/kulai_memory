from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from kulai_db import DbConfig
from kulai_transcription import (
    AudioInputKind,
    TranscriptionCapabilities,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
)

from kulai_memory.application import MemoryIngestionRetiredError, VoiceSessionEvent
from kulai_memory.database_safety import CheckResult, DoctorReport
from kulai_memory.server import (
    ServerConfigurationError,
    ServerDatabaseError,
    VoiceMemoryServerRuntime,
)
from kulai_memory.settings import Settings


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_count = 0

    async def dispose(self) -> None:
        self.dispose_count += 1


class NoopSessionFactory:
    def __call__(self):
        raise AssertionError("Persistence is not used by this test.")


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[VoiceSessionEvent] = []

    async def emit(self, event: VoiceSessionEvent) -> None:
        self.events.append(event)


class BlockingProvider:
    provider_id = "blocking-fake"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.PATH}),
        modes=frozenset({TranscriptionMode.TRANSCRIBE}),
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return TranscriptionResult(
                text=f"result-{self.calls}",
                provider_id=self.provider_id,
                model_id="large-v3",
                mode=request.mode,
            )
        finally:
            self.active -= 1


def _runtime(
    *,
    provider: BlockingProvider,
    engine: FakeEngine,
    provider_calls: list[Settings],
    doctor_ok: bool = True,
    settings: Settings | None = None,
) -> VoiceMemoryServerRuntime:
    async def doctor(async_url: str) -> DoctorReport:
        assert async_url.startswith("postgresql+asyncpg://")
        return DoctorReport((CheckResult(name="test", ok=doctor_ok),))

    def provider_factory(active_settings: Settings) -> BlockingProvider:
        provider_calls.append(active_settings)
        return provider

    return VoiceMemoryServerRuntime(
        settings=settings or Settings(_env_file=None),
        provider_factory=provider_factory,
        database_config_factory=lambda: DbConfig(user="u", password="p", name="db"),
        doctor=doctor,
        engine_factory=lambda ignored: engine,
        session_factory_builder=lambda ignored: NoopSessionFactory(),
    )


def test_runtime_reuses_one_provider_and_serializes_inference(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = BlockingProvider()
        engine = FakeEngine()
        provider_calls: list[Settings] = []
        runtime = _runtime(
            provider=provider,
            engine=engine,
            provider_calls=provider_calls,
        )
        await runtime.startup()
        await runtime.startup()
        first_sink = CollectingSink()
        second_sink = CollectingSink()
        first = runtime.create_voice_session(
            event_sink=first_sink,
            session_id=uuid4(),
        )
        second = runtime.create_voice_session(
            event_sink=second_sink,
            session_id=uuid4(),
        )
        await first.start()
        await second.start()
        first_path = tmp_path / "first.wav"
        second_path = tmp_path / "second.wav"
        first_path.write_bytes(b"fixture")
        second_path.write_bytes(b"fixture")

        first_task = asyncio.create_task(
            runtime.transcribe(session=first, audio_path=first_path)
        )
        await provider.started.wait()
        second_task = asyncio.create_task(
            runtime.transcribe(session=second, audio_path=second_path)
        )
        await asyncio.sleep(0)
        assert provider.calls == 1
        assert provider.max_active == 1
        provider.release.set()
        results = await asyncio.gather(first_task, second_task)

        assert len(results) == 2
        assert provider.calls == 2
        assert provider.max_active == 1
        assert runtime.provider is provider
        assert len(provider_calls) == 1
        await runtime.shutdown()
        await runtime.shutdown()
        assert engine.dispose_count == 1

    asyncio.run(scenario())


def test_runtime_rejects_noncanonical_stt_before_creating_provider() -> None:
    async def scenario() -> None:
        provider = BlockingProvider()
        engine = FakeEngine()
        provider_calls: list[Settings] = []
        runtime = _runtime(
            provider=provider,
            engine=engine,
            provider_calls=provider_calls,
            settings=Settings(_env_file=None, kulai_whisper_vad_filter=False),
        )
        with pytest.raises(ServerConfigurationError):
            await runtime.startup()
        assert provider_calls == []
        assert engine.dispose_count == 0

    asyncio.run(scenario())


def test_database_doctor_failure_blocks_provider_and_engine() -> None:
    async def scenario() -> None:
        provider = BlockingProvider()
        engine = FakeEngine()
        provider_calls: list[Settings] = []
        runtime = _runtime(
            provider=provider,
            engine=engine,
            provider_calls=provider_calls,
            doctor_ok=False,
        )
        with pytest.raises(ServerDatabaseError):
            await runtime.startup()
        assert provider_calls == []
        assert engine.dispose_count == 0

    asyncio.run(scenario())


def test_runtime_preserves_retired_error_and_does_not_commit():
    async def scenario():
        class Session:
            commits = 0
            closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

            async def commit(self):
                self.commits += 1

        class Repository:
            async def create_or_get_by_ingestion_id(self, memory):
                raise MemoryIngestionRetiredError()

        async def doctor(ignored):
            return DoctorReport((CheckResult("test", True),))

        session = Session()
        engine = FakeEngine()
        runtime = VoiceMemoryServerRuntime(
            settings=Settings(_env_file=None), provider_factory=lambda ignored: BlockingProvider(),
            database_config_factory=lambda: DbConfig(user="u", password="p", name="db"),
            doctor=doctor, engine_factory=lambda ignored: engine,
            session_factory_builder=lambda ignored: lambda: session,
            repository_factory=lambda ignored: Repository(),
        )
        await runtime.startup()
        with pytest.raises(MemoryIngestionRetiredError):
            await runtime.ingest(
                transcription=TranscriptionResult(text="synthetic", provider_id="fake"),
                ingestion_id=uuid4(), session_id=uuid4(),
            )
        assert session.closed and session.commits == 0
        await runtime.shutdown()
        assert engine.dispose_count == 1
    asyncio.run(scenario())
