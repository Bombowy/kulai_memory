"""Host transaction owning atomic canonical Memory and vector deletion."""

from __future__ import annotations

from uuid import UUID

from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.deletion import (
    MemoryDeletionError, MemoryDeletionResult, MemoryDeletionService,
)
from .persistence import PostgresMemoryRepository


async def delete_memory(
    *, memory_id: UUID, session_factory: async_sessionmaker[AsyncSession],
) -> MemoryDeletionResult:
    """Return after tombstone and both deletes commit in one PostgreSQL session."""

    if not isinstance(memory_id, UUID):
        raise MemoryDeletionError()
    try:
        async with session_factory() as session:
            async with session.begin():
                service = MemoryDeletionService(
                    repository=PostgresMemoryRepository(db=session),
                    store=PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)),
                )
                result = await service.delete(memory_id)
        return result
    except Exception:
        raise MemoryDeletionError() from None
