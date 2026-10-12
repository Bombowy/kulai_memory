"""Separate question v1 protocol; canonical voice-note v1 stays unchanged."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from kulai_memory.application.events import (
    ErrorEvent, RagStartedEvent, SessionReadyEvent, TranscriptFinalEvent,
)
from .assistant_contract import AssistantResponse
from .memory_ws_protocol import PcmAudioContract, ProtocolMessageError


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1]


class QuestionStartCommand(_Command):
    type: Literal["question.start"]
    audio: PcmAudioContract
    top_k: int = Field(default=5, ge=1, le=20, strict=True)


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


AssistantEvent = SessionReadyEvent | TranscriptFinalEvent | RagStartedEvent | AssistantAnswerEvent | ErrorEvent
