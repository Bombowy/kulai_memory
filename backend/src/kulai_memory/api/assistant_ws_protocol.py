"""Question v1 with optional conversation IDs; canonical voice notes unchanged."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from kulai_memory.application.events import (
    ErrorEvent, RagStartedEvent, SessionReadyEvent, TranscriptFinalEvent,
)
from kulai_memory.conversations import TurnWrite
from .assistant_contract import AssistantResponse
from .memory_ws_protocol import PcmAudioContract, ProtocolMessageError


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1]


class QuestionStartCommand(_Command):
    type: Literal["question.start"]
    audio: PcmAudioContract
    top_k: int = Field(default=5, ge=1, le=20, strict=True)
    conversation_id: UUID | None = None
    request_id: UUID | None = None

    @model_validator(mode="after")
    def conversation_identity_pair(self):
        if (self.conversation_id is None) != (self.request_id is None):
            raise ValueError("Both conversation identities are required.")
        return self


class QuestionStopCommand(_Command):
    type: Literal["question.stop"]


_COMMANDS = TypeAdapter(Annotated[QuestionStartCommand | QuestionStopCommand, Field(discriminator="type")])
MAX_COMMAND_CHARS = 4096


def parse_question_command(text: str):
    if len(text) > MAX_COMMAND_CHARS:
        raise ProtocolMessageError()
    try:
        return _COMMANDS.validate_json(text)
    except ValueError:
        raise ProtocolMessageError() from None


class AssistantAnswerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    type: Literal["assistant.completed"] = "assistant.completed"
    session_id: UUID
    sequence: int = Field(ge=1)
    payload: AssistantResponse


class ConversationAnswerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    type: Literal["conversation.completed"] = "conversation.completed"
    session_id: UUID
    sequence: int = Field(ge=1)
    payload: TurnWrite


AssistantEvent = SessionReadyEvent | TranscriptFinalEvent | RagStartedEvent | AssistantAnswerEvent | ConversationAnswerEvent | ErrorEvent
