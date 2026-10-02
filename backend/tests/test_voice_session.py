from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from kulai_memory.application import (
    EventSink,
    SessionReadyEvent,
    VoiceSession,
    VoiceSessionEvent,
    VoiceSessionEventDeliveryError,
    VoiceSessionEventType,
    VoiceSessionState,
    VoiceSessionStateError,
    event_to_jsonable,
)


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[VoiceSessionEvent] = []

    async def emit(self, event: VoiceSessionEvent) -> None:
        self.events.append(event)


class FailingEventSink:
    async def emit(self, event: VoiceSessionEvent) -> None:
        del event
        raise RuntimeError("password=top-secret")


def test_session_has_stable_unique_identity_and_structural_sink() -> None:
    first_sink = InMemoryEventSink()
    first = VoiceSession(event_sink=first_sink)
    second = VoiceSession(event_sink=InMemoryEventSink())

    assert isinstance(first_sink, EventSink)
    assert isinstance(first.session_id, UUID)
    assert first.session_id == first.session_id
    assert first.session_id != second.session_id
    assert first.state is VoiceSessionState.CREATED


def test_start_emits_ready_and_concurrent_starts_are_idempotent() -> None:
    async def scenario() -> None:
        sink = InMemoryEventSink()
        session = VoiceSession(event_sink=sink)

        await asyncio.gather(session.start(), session.start(), session.start())

        assert session.state is VoiceSessionState.ACTIVE
        assert len(sink.events) == 1
        ready = sink.events[0]
        assert isinstance(ready, SessionReadyEvent)
        assert ready.type is VoiceSessionEventType.SESSION_READY
        assert ready.session_id == session.session_id
        assert ready.sequence == 1

    asyncio.run(scenario())


def test_events_remain_in_awaited_emission_order() -> None:
    async def scenario() -> None:
        sink = InMemoryEventSink()
        session = VoiceSession(event_sink=sink)

        await session.start()
        await sink.emit(
            SessionReadyEvent(session_id=session.session_id, sequence=2)
        )

        assert [event.sequence for event in sink.events] == [1, 2]

    asyncio.run(scenario())


def test_close_is_idempotent_before_and_after_start() -> None:
    async def scenario() -> None:
        never_started = VoiceSession(event_sink=InMemoryEventSink())
        await never_started.close()
        await never_started.close()
        assert never_started.state is VoiceSessionState.CLOSED

        active = VoiceSession(event_sink=InMemoryEventSink())
        await active.start()
        await active.close()
        await active.close()
        assert active.state is VoiceSessionState.CLOSED

    asyncio.run(scenario())


def test_closed_session_rejects_start() -> None:
    async def scenario() -> None:
        session = VoiceSession(event_sink=InMemoryEventSink())
        await session.close()

        with pytest.raises(VoiceSessionStateError) as caught:
            await session.start()

        assert str(caught.value) == VoiceSessionStateError.safe_message
        assert session.state is VoiceSessionState.CLOSED

    asyncio.run(scenario())


def test_delivery_failure_is_controlled_and_public_event_is_safe() -> None:
    async def scenario() -> None:
        session = VoiceSession(event_sink=FailingEventSink())

        with pytest.raises(VoiceSessionEventDeliveryError) as caught:
            await session.start()

        error = caught.value
        assert isinstance(error.__cause__, RuntimeError)
        assert session.state is VoiceSessionState.FAILED

        public_event = error.to_event(session_id=session.session_id, sequence=2)
        serialized = str(event_to_jsonable(public_event))
        assert public_event.type is VoiceSessionEventType.ERROR
        assert public_event.payload.code == "voice_session.event_delivery_failed"
        assert "password" not in serialized
        assert "top-secret" not in serialized

        await session.close()
        with pytest.raises(VoiceSessionStateError):
            await session.start()

    asyncio.run(scenario())
