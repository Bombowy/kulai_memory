"""Lifecycle of one logical user voice session."""

from __future__ import annotations

import asyncio
from enum import Enum
from uuid import UUID, uuid4

from .events import ErrorEvent, ErrorPayload, SessionReadyEvent
from .ports import EventSink


class VoiceSessionState(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class VoiceSessionError(RuntimeError):
    """A controlled error safe to expose at an application boundary."""

    code = "voice_session.error"
    safe_message = "The voice session could not complete the operation."
    recoverable = False

    def __init__(self, *, safe_message: str | None = None) -> None:
        self.public_message = safe_message or self.safe_message
        super().__init__(self.public_message)

    def to_event(self, *, session_id: UUID, sequence: int) -> ErrorEvent:
        """Map the safe public fields to the canonical error event."""

        return ErrorEvent(
            session_id=session_id,
            sequence=sequence,
            payload=ErrorPayload(
                code=self.code,
                message=self.public_message,
                recoverable=self.recoverable,
            ),
        )


class VoiceSessionStateError(VoiceSessionError):
    code = "voice_session.invalid_state"
    safe_message = "The operation is not allowed in the current session state."


class VoiceSessionEventDeliveryError(VoiceSessionError):
    code = "voice_session.event_delivery_failed"
    safe_message = "The voice session could not deliver an event."


class VoiceSession:
    """Transport-neutral lifecycle for one logical user session."""

    def __init__(self, *, event_sink: EventSink) -> None:
        self._event_sink = event_sink
        self._session_id = uuid4()
        self._state = VoiceSessionState.CREATED
        self._sequence = 0
        self._lifecycle_lock = asyncio.Lock()

    @property
    def session_id(self) -> UUID:
        return self._session_id

    @property
    def state(self) -> VoiceSessionState:
        return self._state

    async def start(self) -> None:
        """Activate the session and emit exactly one ``session.ready`` event."""

        async with self._lifecycle_lock:
            if self._state is VoiceSessionState.ACTIVE:
                return
            if self._state is not VoiceSessionState.CREATED:
                raise VoiceSessionStateError()

            self._state = VoiceSessionState.ACTIVE
            event = SessionReadyEvent(
                session_id=self._session_id,
                sequence=self._next_sequence(),
            )
            try:
                await self._event_sink.emit(event)
            except asyncio.CancelledError:
                self._state = VoiceSessionState.FAILED
                raise
            except Exception as exc:
                self._state = VoiceSessionState.FAILED
                raise VoiceSessionEventDeliveryError() from exc

    async def close(self) -> None:
        """Close the session; repeated calls are safe no-ops."""

        async with self._lifecycle_lock:
            if self._state in {VoiceSessionState.CLOSED, VoiceSessionState.FAILED}:
                return
            if self._state is VoiceSessionState.CREATED:
                self._state = VoiceSessionState.CLOSED
                return
            if self._state is not VoiceSessionState.ACTIVE:
                raise VoiceSessionStateError()

            self._state = VoiceSessionState.CLOSING
            self._state = VoiceSessionState.CLOSED

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence
