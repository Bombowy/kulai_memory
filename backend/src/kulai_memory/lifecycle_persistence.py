"""Commit canonical changes before entering automatic embedding/indexing."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.indexing import EnsureMemoryIndexedResult, MemoryIndexingError
from .application.lifecycle import MemoryLifecycleError, MemoryLifecycleResult, MemoryLifecycleService
from .application.memory import MemoryArchivedError
from .automatic_indexing import RuntimeMemoryIndexer
from .persistence import PostgresMemoryRepository


@dataclass(frozen=True, slots=True)
class MemoryChangeResult:
    canonical: MemoryLifecycleResult
    indexing: EnsureMemoryIndexedResult | None = None
    indexing_error_code: str | None = None


async def change_memory(
    *, action: str, memory_id: UUID, session_factory: async_sessionmaker[AsyncSession],
    indexer: RuntimeMemoryIndexer | None = None, content: str | None = None,
    expected_revision: int | None = None,
) -> MemoryChangeResult:
    if action not in {"edit", "archive", "restore"} or (action != "archive" and indexer is None):
        raise MemoryLifecycleError()
    try:
        async with session_factory() as session:
            async with session.begin():
                service = MemoryLifecycleService(repository=PostgresMemoryRepository(db=session),
                    store=PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)))
                if action == "edit":
                    result = await service.edit(memory_id=memory_id, content=content, expected_revision=expected_revision)
                elif action == "archive":
                    result = await service.archive(memory_id=memory_id)
                else:
                    result = await service.restore(memory_id=memory_id)
    except (MemoryLifecycleError, MemoryArchivedError):
        raise
    except Exception:
        raise MemoryLifecycleError() from None
    # Canonical COMMIT succeeded and the session is closed. Cancellation must
    # propagate; a crash leaves durable missing-index state for reconciliation.
    if action == "archive":
        return MemoryChangeResult(result)
    try:
        indexed = await indexer.ensure(memory=result.memory)
        return MemoryChangeResult(result, indexing=indexed)
    except MemoryIndexingError as exc:
        return MemoryChangeResult(result, indexing_error_code=exc.code)
    except Exception:
        return MemoryChangeResult(result, indexing_error_code=MemoryIndexingError.code)
