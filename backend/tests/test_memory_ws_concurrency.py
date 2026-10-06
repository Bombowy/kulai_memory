from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from kulai_db import DbConfig
from kulai_transcription import (
    AudioInputKind,
    TranscriptionCapabilities,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
)

from kulai_memory.app_factory import create_app
from kulai_memory.application import IdempotentMemoryWrite, Memory
from kulai_memory.database_safety import CheckResult, DoctorReport
from kulai_memory.server import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings


class BlockingProvider:
    provider_id = "concurrent-websocket-fake"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.PATH}),
        modes=frozenset({TranscriptionMode.TRANSCRIBE}),
    )

    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.first_started.set()
        try:
            await asyncio.to_thread(self.release.wait)
            return TranscriptionResult(
                text=f"concurrent transcript {self.calls}",
                provider_id=self.provider_id,
                model_id="large-v3",
                mode=request.mode,
            )
        finally:
            self.active -= 1


class InMemoryRepository:
    def __init__(self) -> None:
        self.memories: dict[UUID, Memory] = {}
        self._lock = asyncio.Lock()

    async def create(self, memory: Memory) -> Memory:
        self.memories[memory.ingestion_id] = memory
        return memory

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        async with self._lock:
            existing = self.memories.get(memory.ingestion_id)
            if existing is not None:
                return IdempotentMemoryWrite(memory=existing, created=False)
            self.memories[memory.ingestion_id] = memory
            return IdempotentMemoryWrite(memory=memory, created=True)

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        return next(
            (memory for memory in self.memories.values() if memory.id == memory_id),
            None,
        )

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        return tuple(self.memories.values())[:limit]


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_count = 0

    async def dispose(self) -> None:
        self.dispose_count += 1


def _start(ingestion_id: UUID) -> dict[str, object]:
    return {
        "schema_version": 1,
        "type": "recording.start",
        "ingestion_id": str(ingestion_id),
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
        },
    }


def test_two_websocket_connections_share_one_serialized_provider() -> None:
    provider = BlockingProvider()
    repository = InMemoryRepository()
    engine = FakeEngine()
    provider_factory_calls = 0
    runtime_factory_calls = 0

    async def doctor(async_url: str) -> DoctorReport:
        assert async_url.startswith("postgresql+asyncpg://")
        return DoctorReport((CheckResult(name="test", ok=True),))

    def provider_factory(settings: Settings) -> BlockingProvider:
        nonlocal provider_factory_calls
        provider_factory_calls += 1
        assert settings.kulai_whisper_model == "large-v3"
        return provider

    def runtime_factory() -> VoiceMemoryServerRuntime:
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        return VoiceMemoryServerRuntime(
            settings=Settings(_env_file=None),
            provider_factory=provider_factory,
            database_config_factory=lambda: DbConfig(
                user="u", password="p", name="db"
            ),
            doctor=doctor,
            engine_factory=lambda ignored: engine,
            session_factory_builder=lambda ignored: FakeSession,
            repository_factory=lambda ignored: repository,
        )

    app = create_app(voice_runtime_factory=runtime_factory)

    def flow(client: TestClient, ingestion_id: UUID) -> tuple[str, str]:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start(ingestion_id))
            ready = websocket.receive_json()
            websocket.send_json({"schema_version": 1, "type": "recording.stop"})
            final = websocket.receive_json()
            saving = websocket.receive_json()
            saved = websocket.receive_json()
            assert [
                ready["type"],
                final["type"],
                saving["type"],
                saved["type"],
            ] == [
                "session.ready",
                "transcript.final",
                "memory.saving",
                "memory.saved",
            ]
            return ready["session_id"], saved["payload"]["memory_id"]

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(flow, client, uuid4())
        assert provider.first_started.wait(timeout=5)
        second = pool.submit(flow, client, uuid4())
        threading.Event().wait(0.1)
        assert provider.calls == 1
        assert provider.max_active == 1
        provider.release.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert provider.calls == 2
    assert provider.max_active == 1
    assert provider_factory_calls == 1
    assert runtime_factory_calls == 1
    assert first_result[0] != second_result[0]
    assert first_result[1] != second_result[1]
    assert len(repository.memories) == 2
    assert engine.dispose_count == 1
