"""Delete canonical Memory and its derived vector in caller-owned storage."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from kulai_vector_store import VectorDeleteRequest, VectorStore, delete as vector_delete

from .indexing import MEMORY_VECTOR_NAMESPACE
from .ports import MemoryDeletionRepository


class MemoryDeletionError(RuntimeError):
    """Public deletion failure without user or infrastructure payloads."""

    def __init__(self) -> None:
        super().__init__("Memory deletion could not be completed.")


@dataclass(frozen=True, slots=True)
class MemoryDeletionResult:
    memory_id: UUID
    memory_deleted: bool
    vector_deleted_count: int


class MemoryDeletionService:
    """Both adapters must share one transaction; the host commits the result.

    Delete canonical Memory first to serialize with host indexing's row lock.
    Missing Memory is allowed, including cleanup of its orphan vector.
    """

    def __init__(
        self, *, repository: MemoryDeletionRepository, store: VectorStore,
    ) -> None:
        self._repository = repository
        self._store = store

    async def delete(self, memory_id: UUID) -> MemoryDeletionResult:
        try:
            if not isinstance(memory_id, UUID):
                raise MemoryDeletionError()
            memory_deleted = await self._repository.delete_by_id(memory_id)
            if type(memory_deleted) is not bool:
                raise MemoryDeletionError()
            result = await vector_delete(
                store=self._store,
                request=VectorDeleteRequest(
                    ids=(str(memory_id),), namespace=MEMORY_VECTOR_NAMESPACE,
                ),
            )
            return MemoryDeletionResult(memory_id, memory_deleted, result.deleted_count)
        except Exception:
            raise MemoryDeletionError() from None
