"""Bounded crash-gap repair and one reusable embedding client per runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from kulai_embeddings import EmbeddingProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.indexing import (
    MEMORY_VECTOR_NAMESPACE,
    EnsureMemoryIndexedResult,
    IndexReconciliationReport,
    MemoryIndexingError,
    MemoryIndexingMissingError,
    MemoryIndexingService,
    MemoryIndexingState,
)
from .application.memory import Memory
from .embedding_provider import create_embedding_provider
from .indexing_persistence import (
    AUTOMATIC_EMBEDDING_DIMENSION, AUTOMATIC_EMBEDDING_MODEL,
    ensure_memory_indexed,
)
from .persistence import PostgresMemoryRepository
from .settings import Settings


class OwnedEmbeddingProvider(EmbeddingProvider, Protocol):
    async def aclose(self) -> None: ...


EmbeddingProviderFactory = Callable[..., OwnedEmbeddingProvider]
STARTUP_RECONCILIATION_LIMIT = 100


class RuntimeMemoryIndexer(Protocol):
    async def ensure(self, *, memory: Memory) -> EnsureMemoryIndexedResult: ...
    async def reconcile(self, *, limit: int = STARTUP_RECONCILIATION_LIMIT) -> IndexReconciliationReport: ...
    async def aclose(self) -> None: ...


RuntimeIndexerFactory = Callable[..., RuntimeMemoryIndexer]


class AutomaticMemoryIndexer:
    """Own client lifecycle; canonical Memory itself is the durable outbox."""

    def __init__(
        self, *, settings: Settings, session_factory: async_sessionmaker[AsyncSession],
        provider_factory: EmbeddingProviderFactory = create_embedding_provider,
        embedding_timeout_seconds: float = 120.0,
    ) -> None:
        if (
            settings.kulai_embedding_model != AUTOMATIC_EMBEDDING_MODEL
            or settings.kulai_vector_dimension != AUTOMATIC_EMBEDDING_DIMENSION
        ):
            raise MemoryIndexingError()
        self._factory = session_factory
        self._timeout = embedding_timeout_seconds
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._provider = provider_factory(settings=settings)
        self._service = MemoryIndexingService(
            provider=self._provider, expected_dimension=AUTOMATIC_EMBEDDING_DIMENSION,
        )

    @property
    def provider(self) -> OwnedEmbeddingProvider:
        return self._provider

    def _require_open(self) -> None:
        if self._closed:
            raise MemoryIndexingError()

    async def _ensure(self, memory: Memory) -> EnsureMemoryIndexedResult:
        self._require_open()
        return await ensure_memory_indexed(
            memory=memory, service=self._service, session_factory=self._factory,
            embedding_timeout_seconds=self._timeout,
        )

    async def ensure(self, *, memory: Memory) -> EnsureMemoryIndexedResult:
        async with self._lock:
            self._require_open()
            return await self._ensure(memory)

    async def _selection(self, limit: int) -> tuple[tuple[UUID, ...], int, int, int]:
        async with self._factory() as session:
            try:
                await session.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                counts = (await session.execute(text("""
                    SELECT count(*) FILTER (WHERE v.pk IS NULL) AS missing,
                        count(*) FILTER (WHERE v.pk IS NOT NULL AND
                            v.metadata_json @> jsonb_build_object(
                                'source_memory_id', CAST(m.id AS text),
                                'embedding_provider_id', 'ollama',
                                'embedding_model_tag', CAST(:model AS text),
                                'embedding_dimension', 1024)
                            AND v.metadata_json->>'embedding_dimension' = '1024') AS compatible,
                        count(*) FILTER (WHERE v.pk IS NOT NULL) AS existing
                    FROM memories m LEFT JOIN kulai_vector_records v
                      ON v.namespace_key = :namespace AND v.record_id = CAST(m.id AS text)
                """), {"namespace": MEMORY_VECTOR_NAMESPACE, "model": AUTOMATIC_EMBEDDING_MODEL})).mappings().one()
                ids = tuple((await session.execute(text("""
                    SELECT m.id FROM memories m WHERE NOT EXISTS (
                        SELECT 1 FROM kulai_vector_records v
                        WHERE v.namespace_key = :namespace AND v.record_id = CAST(m.id AS text)
                    ) ORDER BY m.created_at ASC, m.id ASC LIMIT :limit
                """), {"namespace": MEMORY_VECTOR_NAMESPACE, "limit": limit})).scalars().all())
                return ids, counts["compatible"], counts["existing"] - counts["compatible"], counts["missing"]
            finally:
                await session.rollback()

    async def _read_memory(self, memory_id: UUID) -> Memory | None:
        async with self._factory() as session:
            try:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                return await PostgresMemoryRepository(db=session).get_by_id(memory_id)
            finally:
                await session.rollback()

    async def reconcile(self, *, limit: int = STARTUP_RECONCILIATION_LIMIT) -> IndexReconciliationReport:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise MemoryIndexingError()
        async with self._lock:
            self._require_open()
            selected = indexed = already = deleted = failed = compatible = incompatible = remaining = 0
            error_code = None
            try:
                ids, compatible, incompatible, remaining = await self._selection(limit)
                selected = len(ids)
                for memory_id in ids:
                    try:
                        memory = await self._read_memory(memory_id)
                        if memory is None:
                            deleted += 1
                            continue
                        result = await self._ensure(memory)
                        if result.state is MemoryIndexingState.INDEXED:
                            indexed += 1
                        else:
                            already += 1
                    except MemoryIndexingMissingError:
                        deleted += 1
                    except MemoryIndexingError as exc:
                        failed += 1
                        error_code = exc.code
                        break
                _, compatible, incompatible, remaining = await self._selection(1)
            except Exception:
                failed += 1
                error_code = MemoryIndexingError.code
            return IndexReconciliationReport(
                selected=selected, indexed=indexed, already_indexed=already,
                compatible_existing=compatible, deleted_skipped=deleted, failed=failed,
                incompatible_existing=incompatible, remaining_missing=remaining, error_code=error_code,
            )

    async def _close_provider(self) -> None:
        async with self._lock:
            await self._provider.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_provider())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise
