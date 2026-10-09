"""Ports implemented by adapters around the application core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from .events import VoiceSessionEvent

if TYPE_CHECKING:
    from .memory import IdempotentMemoryWrite, Memory


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

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        """Atomically create Memory or return the row for its ingestion UUID."""

        ...

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        ...

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        ...


@runtime_checkable
class MemoryLibraryRepository(Protocol):
    """Bounded canonical reads with an explicit active/archived partition."""

    async def list_library(self, *, archived: bool, limit: int) -> tuple[Memory, ...]:
        """Order by created_at DESC, id DESC; include only the requested partition."""
        ...


@runtime_checkable
class MemoryDeletionRepository(Protocol):
    """Delete canonical Memory within the caller's shared transaction."""

    async def delete_by_id(self, memory_id: UUID) -> bool:
        """Return whether one Memory was deleted, without committing."""

        ...
