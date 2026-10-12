"""One ephemeral voice question per connection, using the shared server runtime."""
import asyncio
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from kulai_memory.application.events import (
    ErrorEvent, ErrorPayload, RagStartedEvent, SessionReadyEvent, TranscriptFinalEvent, TranscriptFinalPayload,
)
from kulai_memory.server.runtime import ServerRuntimeError
from .assistant_contract import AssistantResponse
from .assistant_ws_protocol import (
    AssistantAnswerEvent, QuestionStartCommand, QuestionStopCommand, parse_question_command,
)
from .cancellation import ClientDisconnected, until_disconnect
from .memory_ws_protocol import ProtocolMessageError
from .pcm_wav import OwnedPcmWav, PcmFrameError, PcmAudioLimitError
from .server_access import server_runtime


class _QuestionConnection:
    def __init__(self, websocket: WebSocket):
        self.socket = websocket
        self.session_id = uuid4()
        self.sequence = 0
        self.wav = None

    async def emit(self, event_type, **kwargs):
        self.sequence += 1
        event = event_type(session_id=self.session_id, sequence=self.sequence, **kwargs)
        await self.socket.send_json(event.model_dump(mode="json"))

    async def fail(self, *, code: str, message: str, close: int):
        try:
            await self.emit(ErrorEvent, payload=ErrorPayload(code=code, message=message, recoverable=False))
            await self.socket.close(code=close)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    def cleanup(self):
        wav, self.wav = self.wav, None
        if wav is not None:
            wav.cleanup()

    async def process(self, runtime, top_k):
        """Keep disconnect monitoring active across STT, events and RAG."""
        path = self.wav.finish()
        result = await runtime.transcribe_question(audio_path=path)
        # Whisper has stopped reading. Audio is gone before RAG starts.
        self.cleanup()
        await self.emit(TranscriptFinalEvent, payload=TranscriptFinalPayload(text=result.text))
        if not result.text.strip():
            await self.socket.close(code=1000)
            return
        await self.emit(RagStartedEvent)
        answer = await runtime.ask_memory(query=result.text, top_k=top_k)
        await self.emit(AssistantAnswerEvent, payload=AssistantResponse.from_result(answer))
        await self.socket.close(code=1000)

    async def run(self):
        try:
            runtime = server_runtime(self.socket.app)
            top_k = None
            while True:
                message = await self.socket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                binary = message.get("bytes")
                if binary is not None:
                    if self.wav is None:
                        raise ProtocolMessageError()
                    self.wav.write(binary)
                    continue
                text = message.get("text")
                if text is None:
                    raise ProtocolMessageError()
                command = parse_question_command(text)
                if isinstance(command, QuestionStartCommand):
                    if self.wav is not None:
                        raise ProtocolMessageError()
                    self.wav = OwnedPcmWav()
                    top_k = command.top_k
                    await self.emit(SessionReadyEvent)
                elif isinstance(command, QuestionStopCommand):
                    if self.wav is None:
                        raise ProtocolMessageError()
                    if self.wav.byte_count == 0:
                        self.cleanup()
                        await self.emit(TranscriptFinalEvent, payload=TranscriptFinalPayload(text=""))
                        await self.socket.close(code=1000)
                        return
                    await until_disconnect(self.process(runtime, top_k), self.socket.receive,
                                           protocol_error=ProtocolMessageError)
                    return
        except PcmAudioLimitError:
            await self.fail(code="audio.limit_exceeded", message="Question audio exceeds the allowed limit.", close=1009)
        except (ProtocolMessageError, PcmFrameError):
            await self.fail(code="protocol.invalid_message", message="Invalid assistant request.", close=1002)
        except ServerRuntimeError:
            await self.fail(code="server.unavailable", message="Memory assistant is unavailable.", close=1013)
        except (WebSocketDisconnect, ClientDisconnected):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.fail(code="assistant.operation_failed",
                            message="Memory assistant could not complete the operation.", close=1011)
        finally:
            self.cleanup()


def create_assistant_ws_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/assistant")
    async def assistant(websocket: WebSocket):
        await websocket.accept()
        await _QuestionConnection(websocket).run()

    return router
