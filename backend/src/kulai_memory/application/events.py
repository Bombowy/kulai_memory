"""Canonical event contract shared by local and remote adapters."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter


class _EventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )


class VoiceSessionEventType(str, Enum):
    SESSION_READY = "session.ready"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    MEMORY_SAVING = "memory.saving"
    MEMORY_SAVED = "memory.saved"
    RAG_STARTED = "rag.started"
    RAG_CONTEXT = "rag.context"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_COMPLETED = "assistant.completed"
    ERROR = "error"


class SessionReadyPayload(_EventModel):
    """Payload for a session that is ready to accept work."""


class TranscriptPartialPayload(_EventModel):
    text: str


class TranscriptFinalPayload(_EventModel):
    text: str


class MemorySavingPayload(_EventModel):
    """Payload emitted when memory persistence begins."""


class MemorySavedPayload(_EventModel):
    memory_id: str


class RagStartedPayload(_EventModel):
    """Payload emitted when RAG context retrieval begins."""


class RagContextPayload(_EventModel):
    context: str


class AssistantDeltaPayload(_EventModel):
    delta: str


class AssistantCompletedPayload(_EventModel):
    text: str


class ErrorPayload(_EventModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class _VoiceSessionEvent(_EventModel):
    schema_version: Literal[1] = 1
    session_id: UUID
    sequence: int = Field(ge=1)


class SessionReadyEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.SESSION_READY] = (
        VoiceSessionEventType.SESSION_READY
    )
    payload: SessionReadyPayload = Field(default_factory=SessionReadyPayload)


class TranscriptPartialEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.TRANSCRIPT_PARTIAL] = (
        VoiceSessionEventType.TRANSCRIPT_PARTIAL
    )
    payload: TranscriptPartialPayload


class TranscriptFinalEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.TRANSCRIPT_FINAL] = (
        VoiceSessionEventType.TRANSCRIPT_FINAL
    )
    payload: TranscriptFinalPayload


class MemorySavingEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.MEMORY_SAVING] = (
        VoiceSessionEventType.MEMORY_SAVING
    )
    payload: MemorySavingPayload = Field(default_factory=MemorySavingPayload)


class MemorySavedEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.MEMORY_SAVED] = (
        VoiceSessionEventType.MEMORY_SAVED
    )
    payload: MemorySavedPayload


class RagStartedEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.RAG_STARTED] = VoiceSessionEventType.RAG_STARTED
    payload: RagStartedPayload = Field(default_factory=RagStartedPayload)


class RagContextEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.RAG_CONTEXT] = VoiceSessionEventType.RAG_CONTEXT
    payload: RagContextPayload


class AssistantDeltaEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.ASSISTANT_DELTA] = (
        VoiceSessionEventType.ASSISTANT_DELTA
    )
    payload: AssistantDeltaPayload


class AssistantCompletedEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.ASSISTANT_COMPLETED] = (
        VoiceSessionEventType.ASSISTANT_COMPLETED
    )
    payload: AssistantCompletedPayload


class ErrorEvent(_VoiceSessionEvent):
    type: Literal[VoiceSessionEventType.ERROR] = VoiceSessionEventType.ERROR
    payload: ErrorPayload


VoiceSessionEvent = Annotated[
    SessionReadyEvent
    | TranscriptPartialEvent
    | TranscriptFinalEvent
    | MemorySavingEvent
    | MemorySavedEvent
    | RagStartedEvent
    | RagContextEvent
    | AssistantDeltaEvent
    | AssistantCompletedEvent
    | ErrorEvent,
    Field(discriminator="type"),
]

_EVENT_ADAPTER = TypeAdapter(VoiceSessionEvent)


def event_to_jsonable(event: VoiceSessionEvent) -> dict[str, JsonValue]:
    """Return a JSON-compatible representation for a transport adapter."""

    return cast(
        dict[str, JsonValue],
        _EVENT_ADAPTER.dump_python(event, mode="json"),
    )
