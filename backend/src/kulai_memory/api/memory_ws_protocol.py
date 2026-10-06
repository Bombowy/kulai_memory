"""Typed client commands for the local WebSocket voice-memory protocol."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class _ProtocolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )


class PcmAudioContract(_ProtocolModel):
    encoding: Literal["pcm_s16le"]
    sample_rate: Literal[16000]
    channels: Literal[1]


class RecordingStartCommand(_ProtocolModel):
    schema_version: Literal[1] = 1
    type: Literal["recording.start"]
    ingestion_id: UUID
    audio: PcmAudioContract


class RecordingStopCommand(_ProtocolModel):
    schema_version: Literal[1] = 1
    type: Literal["recording.stop"]


class MemoryRetryCommand(_ProtocolModel):
    schema_version: Literal[1] = 1
    type: Literal["memory.retry"]


ClientCommand = Annotated[
    RecordingStartCommand | RecordingStopCommand | MemoryRetryCommand,
    Field(discriminator="type"),
]

_CLIENT_COMMAND_ADAPTER = TypeAdapter(ClientCommand)


class ProtocolMessageError(ValueError):
    """A client message does not match protocol v1."""


def parse_client_command(text: str) -> ClientCommand:
    """Parse one text frame without exposing validation input in errors."""

    try:
        payload = json.loads(text)
        return _CLIENT_COMMAND_ADAPTER.validate_python(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ProtocolMessageError from exc


__all__ = [
    "ClientCommand",
    "MemoryRetryCommand",
    "PcmAudioContract",
    "ProtocolMessageError",
    "RecordingStartCommand",
    "RecordingStopCommand",
    "parse_client_command",
]
