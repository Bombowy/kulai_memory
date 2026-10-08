"""FastAPI WebSocket adapter for one voice memory per connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from kulai_transcription import TranscriptionResult

from kulai_memory.application import (
    ErrorEvent,
    ErrorPayload,
    EventSink,
    MemoryIdempotencyConflictError,
    MemoryIngestionRetiredError,
    MemorySavedEvent,
    MemorySavedPayload,
    MemorySavingEvent,
    TranscriptMemoryIngestionResult,
    VoiceSession,
    VoiceSessionError,
    VoiceSessionEvent,
    VoiceSessionEventDeliveryError,
    event_to_jsonable,
    transcription_is_empty,
)
from kulai_memory.server import (
    ServerPersistenceError,
    ServerIndexingError,
    ServerRuntimeError,
    VoiceMemoryServerRuntime,
)

from .memory_ws_protocol import (
    MemoryRetryCommand,
    ProtocolMessageError,
    RecordingStartCommand,
    RecordingStopCommand,
    parse_client_command,
)
from .pcm_wav import OwnedPcmWav, PcmAudioLimitError, PcmFrameError


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    RECORDING = "recording"
    PROCESSING = "processing"
    PENDING_SAVE = "pending_save"
    COMPLETED = "completed"


class VoiceServerRuntime(Protocol):
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
    def create_voice_session(
        self, *, event_sink: EventSink, session_id: UUID
    ) -> VoiceSession: ...
    async def transcribe(
        self, *, session: VoiceSession, audio_path: Path
    ) -> TranscriptionResult: ...
    async def ingest(
        self,
        *,
        transcription: TranscriptionResult,
        ingestion_id: UUID,
        session_id: UUID,
    ) -> TranscriptMemoryIngestionResult: ...


RuntimeFactory = Callable[[], VoiceServerRuntime]


@dataclass(frozen=True, slots=True)
class _RuntimeState:
    runtime: VoiceServerRuntime | None
    error: ServerRuntimeError | None


@dataclass(frozen=True, slots=True)
class _PendingSave:
    transcription: TranscriptionResult
    ingestion_id: UUID
    session_id: UUID


def websocket_session_id(ingestion_id: UUID) -> UUID:
    """Return a server-created stable session identity for reconnect retries."""

    return uuid5(
        NAMESPACE_URL,
        f"kulai-memory:websocket-session:v1:{ingestion_id}",
    )


class WebSocketEventSink:
    """Ordered structural EventSink backed by one FastAPI WebSocket."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._session_id: UUID | None = None
        self._last_sequence = 0
        self._send_lock = asyncio.Lock()

    @property
    def session_id(self) -> UUID | None:
        return self._session_id

    @property
    def next_sequence(self) -> int:
        return self._last_sequence + 1

    def bind(self, session_id: UUID) -> None:
        if self._session_id is not None and self._session_id != session_id:
            raise RuntimeError("WebSocket event sink is already bound.")
        self._session_id = session_id

    async def emit(self, event: VoiceSessionEvent) -> None:
        async with self._send_lock:
            if self._session_id is None:
                self._session_id = event.session_id
            if event.session_id != self._session_id:
                raise RuntimeError("WebSocket event session changed.")
            if event.sequence != self._last_sequence + 1:
                raise RuntimeError("WebSocket event sequence is not contiguous.")
            await self._websocket.send_json(event_to_jsonable(event))
            self._last_sequence = event.sequence


class _MemoryWebSocketConnection:
    def __init__(self, *, websocket: WebSocket, runtime: VoiceServerRuntime) -> None:
        self._websocket = websocket
        self._runtime = runtime
        self._sink = WebSocketEventSink(websocket)
        self._state = ConnectionState.CONNECTED
        self._wav: OwnedPcmWav | None = None
        self._session: VoiceSession | None = None
        self._ingestion_id: UUID | None = None
        self._pending: _PendingSave | None = None
        self._server_closed = False

    async def run(self) -> None:
        try:
            while self._state is not ConnectionState.COMPLETED:
                message = await self._websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    return
                binary = message.get("bytes")
                text = message.get("text")
                if binary is not None:
                    await self._handle_binary(binary)
                elif text is not None:
                    await self._handle_text(text)
                else:
                    await self._protocol_failure()
        except (WebSocketDisconnect, VoiceSessionEventDeliveryError):
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._internal_failure()
        finally:
            await self._cleanup()

    async def _handle_text(self, raw_text: str) -> None:
        try:
            command = parse_client_command(raw_text)
        except ProtocolMessageError:
            await self._protocol_failure()
            return

        if isinstance(command, RecordingStartCommand):
            if self._state is not ConnectionState.CONNECTED:
                await self._protocol_failure()
                return
            await self._start(command)
            return
        if isinstance(command, RecordingStopCommand):
            if self._state is not ConnectionState.RECORDING:
                await self._protocol_failure()
                return
            await self._stop_and_process()
            return
        if isinstance(command, MemoryRetryCommand):
            if self._state is not ConnectionState.PENDING_SAVE:
                await self._protocol_failure()
                return
            await self._attempt_save()
            return
        await self._protocol_failure()

    async def _start(self, command: RecordingStartCommand) -> None:
        session_id = websocket_session_id(command.ingestion_id)
        self._sink.bind(session_id)
        try:
            wav = OwnedPcmWav()
            self._wav = wav
            session = self._runtime.create_voice_session(
                event_sink=self._sink,
                session_id=session_id,
            )
            self._session = session
            self._ingestion_id = command.ingestion_id
            await session.start()
            self._state = ConnectionState.RECORDING
        except Exception:
            await self._internal_failure()

    async def _handle_binary(self, data: bytes) -> None:
        if self._state is not ConnectionState.RECORDING or self._wav is None:
            await self._protocol_failure()
            return
        try:
            self._wav.write(data)
        except PcmAudioLimitError:
            await self._send_error(
                code="protocol.audio_limit_exceeded",
                message="The audio limit was exceeded.",
                recoverable=False,
            )
            self._state = ConnectionState.COMPLETED
            await self._close(1009)
        except PcmFrameError:
            await self._protocol_failure()

    async def _stop_and_process(self) -> None:
        wav = self._wav
        session = self._session
        ingestion_id = self._ingestion_id
        if wav is None or session is None or ingestion_id is None:
            await self._protocol_failure()
            return
        self._state = ConnectionState.PROCESSING
        audio_path = wav.finish()
        try:
            transcription = await self._runtime.transcribe(
                session=session,
                audio_path=audio_path,
            )
        except VoiceSessionError as exc:
            await self._send_error(
                code=exc.code,
                message=exc.public_message,
                recoverable=False,
            )
            self._state = ConnectionState.COMPLETED
            await self._close(1011)
            return
        finally:
            wav.cleanup()
            self._wav = None

        if transcription_is_empty(transcription):
            self._state = ConnectionState.COMPLETED
            await self._close(1000)
            return

        self._pending = _PendingSave(
            transcription=transcription,
            ingestion_id=ingestion_id,
            session_id=session.session_id,
        )
        await self._attempt_save()

    async def _attempt_save(self) -> None:
        pending = self._pending
        if pending is None:
            await self._protocol_failure()
            return
        await self._sink.emit(
            MemorySavingEvent(
                session_id=pending.session_id,
                sequence=self._sink.next_sequence,
            )
        )
        try:
            result = await self._runtime.ingest(
                transcription=pending.transcription,
                ingestion_id=pending.ingestion_id,
                session_id=pending.session_id,
            )
        except MemoryIngestionRetiredError:
            self._pending = None
            await self._send_error(
                code=MemoryIngestionRetiredError.code,
                message=MemoryIngestionRetiredError.safe_message,
                recoverable=False,
            )
            self._state = ConnectionState.COMPLETED
            await self._close(1008)
            return
        except MemoryIdempotencyConflictError:
            await self._send_error(
                code="memory.idempotency_conflict",
                message="The ingestion identifier conflicts with an existing memory.",
                recoverable=False,
            )
            self._state = ConnectionState.COMPLETED
            await self._close(1008)
            return
        except (ServerPersistenceError, ServerIndexingError) as exc:
            await self._send_error(
                code=exc.code,
                message=exc.public_message,
                recoverable=True,
            )
            self._state = ConnectionState.PENDING_SAVE
            return

        memory = result.memory
        if memory is None:
            await self._internal_failure()
            return
        await self._sink.emit(
            MemorySavedEvent(
                session_id=pending.session_id,
                sequence=self._sink.next_sequence,
                payload=MemorySavedPayload(memory_id=str(memory.id)),
            )
        )
        self._pending = None
        self._state = ConnectionState.COMPLETED
        await self._close(1000)

    async def _protocol_failure(self) -> None:
        await self._send_error(
            code="protocol.invalid_message",
            message="The WebSocket message does not match protocol v1.",
            recoverable=False,
        )
        self._state = ConnectionState.COMPLETED
        await self._close(1002)

    async def _internal_failure(self) -> None:
        if self._state is ConnectionState.COMPLETED:
            return
        try:
            await self._send_error(
                code="server.internal_error",
                message="The voice-memory operation could not be completed.",
                recoverable=False,
            )
        except Exception:
            pass
        self._state = ConnectionState.COMPLETED
        await self._close(1011)

    async def _send_error(
        self, *, code: str, message: str, recoverable: bool
    ) -> None:
        if self._sink.session_id is None:
            self._sink.bind(uuid4())
        session_id = self._sink.session_id
        if session_id is None:
            return
        await self._sink.emit(
            ErrorEvent(
                session_id=session_id,
                sequence=self._sink.next_sequence,
                payload=ErrorPayload(
                    code=code,
                    message=message,
                    recoverable=recoverable,
                ),
            )
        )

    async def _close(self, code: int) -> None:
        if self._server_closed:
            return
        self._server_closed = True
        try:
            await self._websocket.close(code=code)
        except Exception:
            pass

    async def _cleanup(self) -> None:
        wav = self._wav
        self._wav = None
        if wav is not None:
            wav.cleanup()
        session = self._session
        self._session = None
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
        self._pending = None


async def _send_runtime_unavailable(
    websocket: WebSocket, error: ServerRuntimeError | None
) -> None:
    await websocket.accept()
    sink = WebSocketEventSink(websocket)
    session_id = uuid4()
    sink.bind(session_id)
    active_error = error or ServerRuntimeError()
    try:
        await sink.emit(
            ErrorEvent(
                session_id=session_id,
                sequence=1,
                payload=ErrorPayload(
                    code=active_error.code,
                    message=active_error.public_message,
                    recoverable=False,
                ),
            )
        )
    finally:
        await websocket.close(code=1013)


def create_memory_ws_router(
    *, runtime_factory: RuntimeFactory = VoiceMemoryServerRuntime
) -> APIRouter:
    """Create the v1 router and its process runtime lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = runtime_factory()
        state: _RuntimeState
        try:
            await runtime.startup()
            state = _RuntimeState(runtime=runtime, error=None)
        except asyncio.CancelledError:
            raise
        except ServerRuntimeError as exc:
            await runtime.shutdown()
            state = _RuntimeState(runtime=None, error=exc)
        except Exception:
            await runtime.shutdown()
            state = _RuntimeState(runtime=None, error=ServerRuntimeError())
        app.state.voice_memory_runtime = state
        try:
            yield
        finally:
            if state.runtime is not None:
                await state.runtime.shutdown()

    router = APIRouter(lifespan=lifespan)

    @router.websocket("/ws/memory")
    async def memory_websocket(websocket: WebSocket) -> None:
        state = getattr(websocket.app.state, "voice_memory_runtime", None)
        if not isinstance(state, _RuntimeState) or state.runtime is None:
            error = state.error if isinstance(state, _RuntimeState) else None
            await _send_runtime_unavailable(websocket, error)
            return
        await websocket.accept()
        connection = _MemoryWebSocketConnection(
            websocket=websocket,
            runtime=state.runtime,
        )
        await connection.run()

    return router


__all__ = [
    "ConnectionState",
    "RuntimeFactory",
    "VoiceServerRuntime",
    "WebSocketEventSink",
    "create_memory_ws_router",
    "websocket_session_id",
]
