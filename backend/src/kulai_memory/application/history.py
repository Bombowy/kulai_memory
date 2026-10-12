"""Neutral, bounded canonical history and detail read models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .memory import Memory


@dataclass(frozen=True)
class MemoryHistoryCursor:
    before_created_at: datetime
    before_id: UUID


@dataclass(frozen=True, repr=False)
class MemoryDetail:
    id: UUID
    created_at: datetime
    revision: int
    archived: bool
    content: str

    @classmethod
    def from_memory(cls, memory: Memory) -> MemoryDetail:
        return cls(memory.id, memory.created_at, memory.revision,
                   memory.archived_at is not None, memory.content)


@dataclass(frozen=True, repr=False)
class MemoryHistoryItem:
    id: UUID
    created_at: datetime
    revision: int
    archived: bool
    content_preview: str

    @classmethod
    def from_memory(cls, memory: Memory) -> MemoryHistoryItem:
        return cls(memory.id, memory.created_at, memory.revision,
                   memory.archived_at is not None, " ".join(memory.content.split())[:200])


@dataclass(frozen=True, repr=False)
class MemoryHistoryPage:
    items: tuple[MemoryHistoryItem, ...]
    next_cursor: MemoryHistoryCursor | None


def validate_history(*, archived: bool, limit: int,
                     cursor: MemoryHistoryCursor | None = None) -> None:
    if type(archived) is not bool or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid Memory history request.")
    if cursor is not None and (
        not isinstance(cursor, MemoryHistoryCursor)
        or not isinstance(cursor.before_id, UUID)
        or not isinstance(cursor.before_created_at, datetime)
        or cursor.before_created_at.utcoffset() is None
    ):
        raise ValueError("Invalid Memory history cursor.")
