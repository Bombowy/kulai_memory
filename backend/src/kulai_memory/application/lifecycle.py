"""Canonical content editing and reversible archive operations.

Adapters share one caller-owned transaction; indexing starts after commit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from kulai_vector_store import VectorDeleteRequest, VectorStore, delete as vector_delete

from .indexing import MEMORY_VECTOR_NAMESPACE
from .memory import Memory, MemoryArchivedError


class MemoryLifecycleError(RuntimeError):
    code = "memory.lifecycle_failed"
    safe_message = "The memory change could not be completed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class MemoryNotFoundError(MemoryLifecycleError):
    code = "memory.not_found"
    safe_message = "The memory no longer exists."


class MemoryRevisionConflictError(MemoryLifecycleError):
    code = "memory.revision_conflict"
    safe_message = "The memory revision has changed. Read it again before editing."


class MemoryMutationRepository(Protocol):
    async def get_for_update(self, memory_id: UUID) -> Memory | None: ...
    async def save_lifecycle(self, memory: Memory) -> Memory: ...


class MemoryLifecycleStatus(str, Enum):
    EDITED = "edited"
    UNCHANGED = "unchanged"
    ARCHIVED = "archived"
    ALREADY_ARCHIVED = "already_archived"
    RESTORED = "restored"
    ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True, slots=True)
class MemoryLifecycleResult:
    memory: Memory
    status: MemoryLifecycleStatus


class MemoryLifecycleService:
    def __init__(self, *, repository: MemoryMutationRepository, store: VectorStore) -> None:
        self._repository = repository
        self._store = store

    async def _current(self, memory_id: UUID) -> Memory:
        if not isinstance(memory_id, UUID):
            raise MemoryLifecycleError()
        memory = await self._repository.get_for_update(memory_id)
        if memory is None:
            raise MemoryNotFoundError()
        return memory

    async def _remove_vector(self, memory_id: UUID) -> None:
        await vector_delete(store=self._store, request=VectorDeleteRequest(
            namespace=MEMORY_VECTOR_NAMESPACE, ids=(str(memory_id),),
        ))

    async def edit(self, *, memory_id: UUID, content: str, expected_revision: int) -> MemoryLifecycleResult:
        try:
            if type(expected_revision) is not int or expected_revision < 1:
                raise MemoryLifecycleError()
            if not isinstance(content, str) or not content.strip():
                raise MemoryLifecycleError()
            memory = await self._current(memory_id)
            if memory.archived_at is not None:
                raise MemoryArchivedError()
            if memory.revision != expected_revision:
                raise MemoryRevisionConflictError()
            if memory.content == content:
                return MemoryLifecycleResult(memory, MemoryLifecycleStatus.UNCHANGED)
            changed = Memory.model_validate({**memory.model_dump(), "content": content, "revision": memory.revision + 1})
            changed = await self._repository.save_lifecycle(changed)
            await self._remove_vector(memory.id)
            return MemoryLifecycleResult(changed, MemoryLifecycleStatus.EDITED)
        except (MemoryLifecycleError, MemoryArchivedError):
            raise
        except Exception:
            raise MemoryLifecycleError() from None

    async def archive(self, *, memory_id: UUID) -> MemoryLifecycleResult:
        try:
            memory = await self._current(memory_id)
            status = MemoryLifecycleStatus.ALREADY_ARCHIVED
            if memory.archived_at is None:
                memory = await self._repository.save_lifecycle(memory.model_copy(update={"archived_at": datetime.now(UTC)}))
                status = MemoryLifecycleStatus.ARCHIVED
            await self._remove_vector(memory.id)
            return MemoryLifecycleResult(memory, status)
        except MemoryLifecycleError:
            raise
        except Exception:
            raise MemoryLifecycleError() from None

    async def restore(self, *, memory_id: UUID) -> MemoryLifecycleResult:
        try:
            memory = await self._current(memory_id)
            if memory.archived_at is None:
                return MemoryLifecycleResult(memory, MemoryLifecycleStatus.ALREADY_ACTIVE)
            memory = await self._repository.save_lifecycle(memory.model_copy(update={"archived_at": None}))
            return MemoryLifecycleResult(memory, MemoryLifecycleStatus.RESTORED)
        except MemoryLifecycleError:
            raise
        except Exception:
            raise MemoryLifecycleError() from None
