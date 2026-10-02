from __future__ import annotations

import json
from uuid import uuid4

from kulai_memory.application import (
    AssistantCompletedEvent,
    AssistantCompletedPayload,
    AssistantDeltaEvent,
    AssistantDeltaPayload,
    ErrorEvent,
    ErrorPayload,
    MemorySavedEvent,
    MemorySavedPayload,
    MemorySavingEvent,
    RagContextEvent,
    RagContextPayload,
    RagStartedEvent,
    SessionReadyEvent,
    TranscriptFinalEvent,
    TranscriptFinalPayload,
    TranscriptPartialEvent,
    TranscriptPartialPayload,
    VoiceSessionEventType,
    event_to_jsonable,
)


def test_every_event_has_a_json_compatible_representation() -> None:
    session_id = uuid4()
    events = [
        SessionReadyEvent(session_id=session_id, sequence=1),
        TranscriptPartialEvent(
            session_id=session_id,
            sequence=2,
            payload=TranscriptPartialPayload(text="par"),
        ),
        TranscriptFinalEvent(
            session_id=session_id,
            sequence=3,
            payload=TranscriptFinalPayload(text="partial"),
        ),
        MemorySavingEvent(session_id=session_id, sequence=4),
        MemorySavedEvent(
            session_id=session_id,
            sequence=5,
            payload=MemorySavedPayload(memory_id="memory-1"),
        ),
        RagStartedEvent(session_id=session_id, sequence=6),
        RagContextEvent(
            session_id=session_id,
            sequence=7,
            payload=RagContextPayload(context="context"),
        ),
        AssistantDeltaEvent(
            session_id=session_id,
            sequence=8,
            payload=AssistantDeltaPayload(delta="answer "),
        ),
        AssistantCompletedEvent(
            session_id=session_id,
            sequence=9,
            payload=AssistantCompletedPayload(text="answer complete"),
        ),
        ErrorEvent(
            session_id=session_id,
            sequence=10,
            payload=ErrorPayload(
                code="voice_session.example",
                message="A safe public message.",
                recoverable=True,
            ),
        ),
    ]

    representations = [event_to_jsonable(event) for event in events]

    assert [item["type"] for item in representations] == [
        event_type.value for event_type in VoiceSessionEventType
    ]
    assert all(item["schema_version"] == 1 for item in representations)
    assert all(item["session_id"] == str(session_id) for item in representations)
    json.dumps(representations)
