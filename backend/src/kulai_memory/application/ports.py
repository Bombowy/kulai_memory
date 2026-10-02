"""Ports implemented by adapters around the application core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from .events import VoiceSessionEvent

if TYPE_CHECKING:
    from .memory import Memory


@runtime_checkable
class EventSink(Protocol):
    """Receives application events without imposing a transport."""

    async def emit(self, event: VoiceSessionEvent) -> None:
        """Deliver one event, preserving the order of awaited calls."""

        ...


@runtime_checkable
class MemoryRepository(Protocol):
    """Persists domain memories without exposing infrastructure models."""

    async def create(self, memory: Memory) -> Memory:
        ...

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        ...

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        ...
