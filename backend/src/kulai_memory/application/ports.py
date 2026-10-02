"""Ports implemented by adapters around the application core."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .events import VoiceSessionEvent


@runtime_checkable
class EventSink(Protocol):
    """Receives application events without imposing a transport."""

    async def emit(self, event: VoiceSessionEvent) -> None:
        """Deliver one event, preserving the order of awaited calls."""

        ...
