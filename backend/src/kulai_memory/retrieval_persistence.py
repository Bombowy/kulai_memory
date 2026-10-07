"""Host-only read transaction after query embedding has completed."""

from __future__ import annotations

from kulai_vector_store import VectorMetric
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.retrieval import (
    MemoryRetrievalError, MemoryRetrievalResult, MemoryRetrievalService,
)
from .persistence import PostgresMemoryRepository


async def retrieve_memories(
    *, query: str, service: MemoryRetrievalService,
    session_factory: async_sessionmaker[AsyncSession], top_k: int = 5,
) -> MemoryRetrievalResult:
    """One consistent read snapshot; caller owns provider and engine lifecycle."""

    request = await service.prepare(query=query, top_k=top_k)
    if request.vector.dimension != 1024:
        raise MemoryRetrievalError("retrieval.dimension_mismatch")
    try:
        async with session_factory() as session:
            try:
                await session.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                store = PgVectorStore(db=session, config=PgVectorStoreConfig(
                    dimension=1024, metric=VectorMetric.COSINE,
                ))
                return await service.search(
                    store=store, repository=PostgresMemoryRepository(db=session), request=request,
                )
            finally:
                await session.rollback()
    except MemoryRetrievalError:
        raise
    except Exception:
        raise MemoryRetrievalError() from None
