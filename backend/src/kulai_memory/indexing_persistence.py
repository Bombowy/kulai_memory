"""Host caller owning short pgvector transactions after embedding completes."""

from __future__ import annotations

from uuid import UUID

from kulai_vector_store import VectorUpsertRequest, VectorUpsertResult
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.indexing import (
    MEMORY_VECTOR_NAMESPACE, MemoryIndexingError, MemoryIndexingService,
)
from .application.memory import Memory
from .persistence import MemoryDb


async def _lock_memory_for_indexing(*, db: AsyncSession, request: VectorUpsertRequest) -> None:
    """Prevent a prepared vector from being written after canonical deletion."""

    if request.namespace != MEMORY_VECTOR_NAMESPACE or len(request.records) != 1:
        raise MemoryIndexingError()
    record_id = request.records[0].id
    memory_id = UUID(record_id)
    if str(memory_id) != record_id:
        raise MemoryIndexingError()
    statement = select(MemoryDb.id).where(MemoryDb.id == memory_id).with_for_update()
    if await db.scalar(statement) != memory_id:
        raise MemoryIndexingError()


async def save_prepared_memory_vector(
    *,
    service: MemoryIndexingService,
    request: VectorUpsertRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> VectorUpsertResult:
    """Commit one prepared vector, rolling back on failure; never change Memory.

    The host caller owns begin/commit/rollback through the transaction context.
    A result is returned only after commit succeeds and the session is closed.
    Lock canonical Memory before touching its vector, in the same order as delete.
    """

    try:
        async with session_factory() as session:
            async with session.begin():
                await _lock_memory_for_indexing(db=session, request=request)
                store = PgVectorStore(
                    db=session, config=PgVectorStoreConfig(dimension=request.dimension)
                )
                result = await service.upsert(store=store, request=request)
        return result
    except Exception:
        raise MemoryIndexingError() from None


async def index_memory(
    *,
    memory: Memory,
    service: MemoryIndexingService,
    session_factory: async_sessionmaker[AsyncSession],
) -> VectorUpsertResult:
    """Embed a detached Memory before opening a new, short write session.

    The caller must already have closed any Memory read transaction. The
    provider's lifecycle belongs to the caller and can span an entire batch.
    """

    request = await service.prepare(memory=memory)
    return await save_prepared_memory_vector(
        service=service, request=request, session_factory=session_factory
    )
