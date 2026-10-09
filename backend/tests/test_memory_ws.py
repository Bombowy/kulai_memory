from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from kulai_transcription import (
    AudioInputKind,
    AudioPathInput,
    TranscriptionCapabilities,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
)
from starlette.websockets import WebSocketDisconnect

from kulai_memory.api import memory_ws
from kulai_memory.api.pcm_wav import OwnedPcmWav
from kulai_memory.app_factory import create_app
from kulai_memory.application import (
    EventSink,
    Memory,
    MemoryIdempotencyConflictError,
    MemoryIngestionRetiredError,
    TranscriptMemoryIngestionResult,
    TranscriptMemoryIngestionStatus,
    TranscriptionService,
    VoiceSession,
    VoiceSessionTranscriptionError,
)
from kulai_memory.server import ServerDatabaseError, ServerPersistenceError, ServerIndexingError


class FakeProvider:
    provider_id = "websocket-fake"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.PATH}),
        modes=frozenset({TranscriptionMode.TRANSCRIBE}),
    )

    def __init__(self, text: str) -> None:
        self.text = text
        self.call_count = 0

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.call_count += 1
        return TranscriptionResult(
            text=self.text,
            provider_id=self.provider_id,
            model_id="large-v3",
            mode=request.mode,
            segments=(),
            duration_seconds=0.1,
        )


class FakeRuntime:
    def __init__(
        self,
        *,
        text: str = "Zapamiętaj spotkanie",
        save_failures: int = 0,
        startup_error: Exception | None = None,
    ) -> None:
        self.provider = FakeProvider(text)
        self.service = TranscriptionService(provider=self.provider)
        self.save_failures = save_failures
        self.startup_error = startup_error
        self.startup_count = 0
        self.shutdown_count = 0
        self.transcribe_count = 0
        self.ingest_calls: list[tuple[UUID, UUID, str]] = []
        self.memories: dict[UUID, Memory] = {}
        self.session_ids: list[UUID] = []

    async def startup(self) -> None:
        self.startup_count += 1
        if self.startup_error is not None:
            raise self.startup_error

    async def shutdown(self) -> None:
        self.shutdown_count += 1

    def create_voice_session(
        self, *, event_sink: EventSink, session_id: UUID
    ) -> VoiceSession:
        self.session_ids.append(session_id)
        return VoiceSession(
            event_sink=event_sink,
            transcription_service=self.service,
            session_id=session_id,
        )

    async def transcribe(
        self, *, session: VoiceSession, audio_path: Path
    ) -> TranscriptionResult:
        assert audio_path.is_file()
        self.transcribe_count += 1
        return await session.transcribe(
            request=TranscriptionRequest(audio=AudioPathInput(path=audio_path))
        )

    async def ingest(
        self,
        *,
        transcription: TranscriptionResult,
        ingestion_id: UUID,
        session_id: UUID,
    ) -> TranscriptMemoryIngestionResult:
        self.ingest_calls.append((ingestion_id, session_id, transcription.text))
        if self.save_failures:
            self.save_failures -= 1
            raise ServerPersistenceError
        existing = self.memories.get(ingestion_id)
        if existing is not None:
            return TranscriptMemoryIngestionResult(
                status=TranscriptMemoryIngestionStatus.DUPLICATE,
                memory=existing,
            )
        memory = Memory(
            ingestion_id=ingestion_id,
            content=transcription.text,
            session_id=session_id,
        )
        self.memories[ingestion_id] = memory
        return TranscriptMemoryIngestionResult(
            status=TranscriptMemoryIngestionStatus.CREATED,
            memory=memory,
        )


def _start_message(ingestion_id: UUID) -> dict[str, object]:
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


def _stop_message() -> dict[str, object]:
    return {"schema_version": 1, "type": "recording.stop"}


def _retry_message() -> dict[str, object]:
    return {"schema_version": 1, "type": "memory.retry"}


def _assert_clean_close(websocket, code: int = 1000) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        websocket.receive_json()
    assert caught.value.code == code


def test_websocket_success_has_canonical_order_and_stable_ingestion_id() -> None:
    runtime = FakeRuntime()
    ingestion_id = uuid4()
    app = create_app(voice_runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(ingestion_id))
            ready = websocket.receive_json()
            websocket.send_bytes(b"\x00\x00" * 32)
            websocket.send_json(_stop_message())
            final = websocket.receive_json()
            saving = websocket.receive_json()
            saved = websocket.receive_json()
            _assert_clean_close(websocket)

    events = [ready, final, saving, saved]
    assert [event["type"] for event in events] == [
        "session.ready",
        "transcript.final",
        "memory.saving",
        "memory.saved",
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert len({event["session_id"] for event in events}) == 1
    assert UUID(saved["payload"]["memory_id"]) == runtime.memories[ingestion_id].id
    assert runtime.ingest_calls[0][0] == ingestion_id
    assert runtime.provider.call_count == 1
    assert runtime.startup_count == runtime.shutdown_count == 1


def test_websocket_empty_transcript_has_no_memory_events() -> None:
    runtime = FakeRuntime(text="   ")
    app = create_app(voice_runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            ready = websocket.receive_json()
            websocket.send_json(_stop_message())
            final = websocket.receive_json()
            _assert_clean_close(websocket)

    assert ready["type"] == "session.ready"
    assert final["type"] == "transcript.final"
    assert final["payload"]["text"].strip() == ""
    assert runtime.ingest_calls == []
    assert runtime.memories == {}


def test_persistence_retry_reuses_transcript_ingestion_and_session() -> None:
    runtime = FakeRuntime(save_failures=1)
    ingestion_id = uuid4()
    app = create_app(voice_runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(ingestion_id))
            ready = websocket.receive_json()
            websocket.send_bytes(b"\x00\x00")
            websocket.send_json(_stop_message())
            final = websocket.receive_json()
            saving_one = websocket.receive_json()
            error = websocket.receive_json()
            websocket.send_json(_retry_message())
            saving_two = websocket.receive_json()
            saved = websocket.receive_json()
            _assert_clean_close(websocket)

    events = [ready, final, saving_one, error, saving_two, saved]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert error["type"] == "error"
    assert error["payload"] == {
        "code": "memory.save_failed",
        "message": "The memory could not be saved. Retry is available.",
        "recoverable": True,
    }
    assert runtime.transcribe_count == runtime.provider.call_count == 1
    assert len(runtime.ingest_calls) == 2
    assert {call[0] for call in runtime.ingest_calls} == {ingestion_id}
    assert len({call[1] for call in runtime.ingest_calls}) == 1
    assert len({event["session_id"] for event in events}) == 1


def test_indexing_retry_preserves_saved_memory_and_sequence_without_second_stt():
    class IndexFailRuntime(FakeRuntime):
        async def ingest(self, **kwargs):
            result = await super().ingest(**kwargs)
            if len(self.ingest_calls) == 1:
                raise ServerIndexingError(memory_id=result.memory.id)
            return result
    runtime = IndexFailRuntime()
    ingestion_id = uuid4()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/memory") as socket:
            socket.send_json(_start_message(ingestion_id))
            events = [socket.receive_json()]
            socket.send_bytes(b"\0\0")
            socket.send_json(_stop_message())
            events.extend(socket.receive_json() for _ in range(3))
            assert events[-1]["payload"]["code"] == "memory.index_failed"
            assert events[-1]["payload"]["recoverable"] is True
            assert "was saved" in events[-1]["payload"]["message"]
            socket.send_json(_retry_message())
            events.extend(socket.receive_json() for _ in range(2))
            _assert_clean_close(socket)
    assert [event["sequence"] for event in events] == list(range(1, 7))
    assert len({event["session_id"] for event in events}) == 1
    assert runtime.transcribe_count == len(runtime.memories) == 1
    assert events[-1]["payload"]["memory_id"] == str(runtime.memories[ingestion_id].id)


@pytest.mark.parametrize(
    ("first_action", "expected_sequence"),
    [
        ("binary", 1),
        ("stop", 1),
        ("retry", 1),
        ("malformed", 1),
    ],
)
def test_invalid_initial_transitions_emit_safe_protocol_error(
    first_action: str, expected_sequence: int
) -> None:
    runtime = FakeRuntime()
    app = create_app(voice_runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            if first_action == "binary":
                websocket.send_bytes(b"\x00\x00")
            elif first_action == "stop":
                websocket.send_json(_stop_message())
            elif first_action == "retry":
                websocket.send_json(_retry_message())
            else:
                websocket.send_text("PRIVATE-SENTINEL-not-json")
            error = websocket.receive_json()
            _assert_clean_close(websocket, 1002)

    assert error["type"] == "error"
    assert error["sequence"] == expected_sequence
    assert error["payload"]["code"] == "protocol.invalid_message"
    assert "PRIVATE-SENTINEL" not in json.dumps(error)


def test_duplicate_start_and_binary_after_stop_are_rejected() -> None:
    duplicate_runtime = FakeRuntime()
    app = create_app(voice_runtime_factory=lambda: duplicate_runtime)
    start = _start_message(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(start)
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_json(start)
            error = websocket.receive_json()
            assert error["sequence"] == 2
            _assert_clean_close(websocket, 1002)

    pending_runtime = FakeRuntime(save_failures=1)
    pending_app = create_app(voice_runtime_factory=lambda: pending_runtime)
    with TestClient(pending_app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            websocket.receive_json()
            websocket.send_json(_stop_message())
            for _ in range(3):
                websocket.receive_json()
            websocket.send_bytes(b"\x00\x00")
            error = websocket.receive_json()
            assert error["sequence"] == 5
            assert error["payload"]["code"] == "protocol.invalid_message"
            _assert_clean_close(websocket, 1002)


def test_odd_oversized_and_total_limited_pcm_are_rejected(monkeypatch) -> None:
    for payload, close_code in (
        (b"x", 1002),
        (b"x" * (256 * 1024 + 2), 1009),
    ):
        runtime = FakeRuntime()
        app = create_app(voice_runtime_factory=lambda: runtime)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/memory") as websocket:
                websocket.send_json(_start_message(uuid4()))
                websocket.receive_json()
                websocket.send_bytes(payload)
                error = websocket.receive_json()
                assert error["type"] == "error"
                _assert_clean_close(websocket, close_code)

    monkeypatch.setattr("kulai_memory.api.pcm_wav.MAX_TOTAL_AUDIO_BYTES", 4)
    runtime = FakeRuntime()
    app = create_app(voice_runtime_factory=lambda: runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            websocket.receive_json()
            websocket.send_bytes(b"\x00" * 4)
            websocket.send_bytes(b"\x00\x00")
            error = websocket.receive_json()
            assert error["payload"]["code"] == "protocol.audio_limit_exceeded"
            _assert_clean_close(websocket, 1009)


def test_disconnect_cleans_owned_temp_wav(monkeypatch) -> None:
    paths: list[Path] = []

    class TrackingWav(OwnedPcmWav):
        def __init__(self) -> None:
            super().__init__()
            paths.append(self.path)

    monkeypatch.setattr(memory_ws, "OwnedPcmWav", TrackingWav)
    runtime = FakeRuntime()
    app = create_app(voice_runtime_factory=lambda: runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            websocket.receive_json()
            websocket.send_bytes(b"\x00\x00")

    assert paths and all(not path.exists() for path in paths)


def test_database_startup_failure_keeps_liveness_and_refuses_voice() -> None:
    runtime = FakeRuntime(startup_error=ServerDatabaseError())
    app = create_app(voice_runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        with client.websocket_connect("/ws/memory") as websocket:
            error = websocket.receive_json()
            assert error["payload"]["code"] == "server.database_unavailable"
            _assert_clean_close(websocket, 1013)

    assert runtime.startup_count == 1
    assert runtime.shutdown_count == 1


def test_transcription_failure_is_safe_and_cleans_temp_wav(
    monkeypatch, caplog
) -> None:
    paths: list[Path] = []

    class TrackingWav(OwnedPcmWav):
        def __init__(self) -> None:
            super().__init__()
            paths.append(self.path)

    class FailingRuntime(FakeRuntime):
        async def transcribe(self, **kwargs) -> TranscriptionResult:
            del kwargs
            try:
                raise RuntimeError("PRIVATE-PROVIDER-SENTINEL")
            except RuntimeError as exc:
                raise VoiceSessionTranscriptionError from exc

    monkeypatch.setattr(memory_ws, "OwnedPcmWav", TrackingWav)
    runtime = FailingRuntime()
    app = create_app(voice_runtime_factory=lambda: runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            websocket.receive_json()
            websocket.send_bytes(b"\x00\x00")
            websocket.send_json(_stop_message())
            error = websocket.receive_json()
            _assert_clean_close(websocket, 1011)

    assert error["sequence"] == 2
    assert error["payload"]["code"] == "voice_session.transcription_failed"
    assert "PRIVATE-PROVIDER-SENTINEL" not in json.dumps(error)
    assert "PRIVATE-PROVIDER-SENTINEL" not in caplog.text
    assert paths and all(not path.exists() for path in paths)
    assert runtime.ingest_calls == []


def test_idempotency_conflict_is_not_retryable() -> None:
    class ConflictRuntime(FakeRuntime):
        async def ingest(self, **kwargs) -> TranscriptMemoryIngestionResult:
            del kwargs
            raise MemoryIdempotencyConflictError

    runtime = ConflictRuntime()
    app = create_app(voice_runtime_factory=lambda: runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            websocket.receive_json()
            websocket.send_json(_stop_message())
            final = websocket.receive_json()
            saving = websocket.receive_json()
            error = websocket.receive_json()
            _assert_clean_close(websocket, 1008)

    assert [final["sequence"], saving["sequence"], error["sequence"]] == [2, 3, 4]
    assert error["payload"]["code"] == "memory.idempotency_conflict"
    assert error["payload"]["recoverable"] is False


@pytest.mark.parametrize("after_save_failure", [False, True])
def test_retired_ingestion_is_terminal_and_never_retranscribes(after_save_failure, monkeypatch, caplog):
    paths = []

    class TrackingWav(OwnedPcmWav):
        def __init__(self):
            super().__init__()
            paths.append(self.path)

    class RetiredRuntime(FakeRuntime):
        async def ingest(self, **kwargs):
            self.ingest_calls.append((kwargs["ingestion_id"], kwargs["session_id"], kwargs["transcription"].text))
            if after_save_failure and len(self.ingest_calls) == 1:
                raise ServerPersistenceError()
            raise MemoryIngestionRetiredError()

    monkeypatch.setattr(memory_ws, "OwnedPcmWav", TrackingWav)
    runtime = RetiredRuntime(text="PRIVATE_RETIRE_TRANSCRIPT_SENTINEL")
    app = create_app(voice_runtime_factory=lambda: runtime)
    identity = uuid4()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(identity))
            events = [websocket.receive_json()]
            websocket.send_bytes(b"\x00\x00")
            websocket.send_json(_stop_message())
            events.extend(websocket.receive_json() for _ in range(3))
            if after_save_failure:
                assert events[-1]["payload"]["recoverable"] is True
                websocket.send_json(_retry_message())
                events.extend(websocket.receive_json() for _ in range(2))
            _assert_clean_close(websocket, 1008)
    error = events[-1]
    assert error["payload"] == {"code": "memory.ingestion_retired",
                               "message": MemoryIngestionRetiredError.safe_message,
                               "recoverable": False}
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len({event["session_id"] for event in events}) == 1
    assert runtime.provider.call_count == runtime.transcribe_count == 1
    assert {call[0] for call in runtime.ingest_calls} == {identity}
    assert runtime.memories == {}
    assert "PRIVATE_RETIRE_TRANSCRIPT_SENTINEL" not in json.dumps(error) + caplog.text
    assert paths and all(not path.exists() for path in paths)


def test_archived_ingestion_is_terminal_generic_error_without_protocol_change():
    from kulai_memory.application import MemoryArchivedError
    class ArchivedRuntime(FakeRuntime):
        async def ingest(self, **kwargs): raise MemoryArchivedError()
    runtime = ArchivedRuntime(text="synthetic archived note")
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/memory") as websocket:
            websocket.send_json(_start_message(uuid4()))
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_bytes(b"\x00\x00")
            websocket.send_json(_stop_message())
            events = [websocket.receive_json() for _ in range(3)]
            assert events[-1]["payload"] == dict(code="memory.archived", message=MemoryArchivedError.safe_message, recoverable=False)
            _assert_clean_close(websocket, 1008)
    assert runtime.transcribe_count == 1
