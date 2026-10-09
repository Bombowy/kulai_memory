"""Host caller owning short pgvector transactions after embedding completes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import UUID

from kulai_vector_store import VectorUpsertRequest, VectorUpsertResult
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.indexing import (
    MEMORY_VECTOR_NAMESPACE, MemoryIndexingError, MemoryIndexingService,
    EnsureMemoryIndexedResult, MemoryIndexingIncompatibleError,
    MemoryIndexingMissingError, MemoryIndexingState, memory_vector_metadata_matches,
    MemoryIndexingArchivedError, MemoryIndexingStaleRevisionError,
)
from .application.memory import Memory
from .persistence import MemoryDb

AUTOMATIC_EMBEDDING_MODEL = "bge-m3:567m-fp16"
AUTOMATIC_EMBEDDING_PROVIDER = "ollama"
AUTOMATIC_EMBEDDING_DIMENSION = 1024


async def _lock_memory_for_indexing(*, db: AsyncSession, request: VectorUpsertRequest) -> None:
    """Prevent a prepared vector from being written after canonical deletion."""

    if request.namespace != MEMORY_VECTOR_NAMESPACE or len(request.records) != 1:
        raise MemoryIndexingError()
    record_id = request.records[0].id
    memory_id = UUID(record_id)
    if str(memory_id) != record_id:
        raise MemoryIndexingError()
    statement = select(MemoryDb).where(MemoryDb.id == memory_id).with_for_update()
    canonical = await db.scalar(statement)
    if canonical is None:
        raise MemoryIndexingMissingError()
    if canonical.archived_at is not None:
        raise MemoryIndexingArchivedError()
    revision = request.records[0].metadata.get("revision")
    if type(revision) is not int or revision != canonical.revision:
        raise MemoryIndexingStaleRevisionError()
    if request.records[0].metadata.get("source_memory_id") != str(canonical.id):
        raise MemoryIndexingIncompatibleError()


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
    except MemoryIndexingError:
        raise
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


def _require_compatible(metadata: Mapping[str, object], memory_id: UUID, revision: int) -> None:
    if not memory_vector_metadata_matches(
        metadata, memory_id=memory_id, provider_id=AUTOMATIC_EMBEDDING_PROVIDER,
        model_tag=AUTOMATIC_EMBEDDING_MODEL, dimension=AUTOMATIC_EMBEDDING_DIMENSION,
        memory_revision=revision,
    ):
        raise MemoryIndexingIncompatibleError()


async def _vector_metadata(db: AsyncSession, memory_id: UUID) -> Mapping[str, object] | None:
    # The reusable store has no get-by-id port. Read metadata only at the host
    # boundary; all writes continue to use its upsert API.
    row = (await db.execute(text(
        "SELECT metadata_json FROM kulai_vector_records "
        "WHERE namespace_key = :namespace AND record_id = :id"
    ), {"namespace": MEMORY_VECTOR_NAMESPACE, "id": str(memory_id)})).one_or_none()
    if row is None:
        return None
    if not isinstance(row[0], Mapping):
        raise MemoryIndexingIncompatibleError()
    return row[0]


async def ensure_memory_indexed(
    *, memory: Memory, service: MemoryIndexingService,
    session_factory: async_sessionmaker[AsyncSession], embedding_timeout_seconds: float = 120.0,
) -> EnsureMemoryIndexedResult:
    """Skip compatible vectors; repair missing ones without implicit reindex.

    The caller has committed and detached Memory. Both checks use the exact
    identity. The second check holds the canonical lock until vector commit.
    Cancellation propagates and the canonical commit is never rolled back.
    """

    try:
        async with session_factory() as session:
            try:
                await session.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                canonical = await session.scalar(select(MemoryDb).where(MemoryDb.id == memory.id))
                if canonical is None:
                    raise MemoryIndexingMissingError()
                if canonical.archived_at is not None:
                    raise MemoryIndexingArchivedError()
                if canonical.revision != memory.revision:
                    raise MemoryIndexingStaleRevisionError()
                existing = await _vector_metadata(session, memory.id)
                if existing is not None:
                    _require_compatible(existing, memory.id, memory.revision)
                    return EnsureMemoryIndexedResult(memory.id, MemoryIndexingState.ALREADY_INDEXED)
            finally:
                await session.rollback()

        async with asyncio.timeout(embedding_timeout_seconds):
            request = await service.prepare(memory=memory)
        if (
            request.namespace != MEMORY_VECTOR_NAMESPACE or len(request.records) != 1
            or request.records[0].id != str(memory.id)
            or request.dimension != AUTOMATIC_EMBEDDING_DIMENSION
            or not any(value != 0.0 for value in request.records[0].vector.values)
        ):
            raise MemoryIndexingError()
        _require_compatible(request.records[0].metadata, memory.id, memory.revision)
        async with session_factory() as session:
            async with session.begin():
                await _lock_memory_for_indexing(db=session, request=request)
                existing = await _vector_metadata(session, memory.id)
                if existing is not None:
                    _require_compatible(existing, memory.id, memory.revision)
                    state = MemoryIndexingState.ALREADY_INDEXED
                else:
                    await service.upsert(store=PgVectorStore(
                        db=session, config=PgVectorStoreConfig(dimension=AUTOMATIC_EMBEDDING_DIMENSION),
                    ), request=request)
                    state = MemoryIndexingState.INDEXED
        return EnsureMemoryIndexedResult(memory.id, state)
    except MemoryIndexingError:
        raise
    except Exception:
        raise MemoryIndexingError() from None
