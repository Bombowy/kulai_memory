"""Host-only read queries and bounded Memory indexing; no automatic trigger."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application import MEMORY_VECTOR_NAMESPACE, MemoryIndexingService
from .database_safety import MemoryFingerprint, VectorFingerprint, memory_fingerprint, vector_fingerprint
from .indexing_persistence import save_prepared_memory_vector
from .persistence import PostgresMemoryRepository

EMBEDDING_MODEL = "bge-m3:567m-fp16"
VECTOR_DIMENSION = 1024


class BackfillError(RuntimeError):
    """Safe operation code; never expose infrastructure exception text."""

    def __init__(self, code: str) -> None:
        super().__init__("Memory backfill could not be completed.")
        self.code = code


@dataclass(frozen=True, slots=True)
class Selection:
    ids: tuple[UUID, ...]
    fingerprints: tuple[MemoryFingerprint, VectorFingerprint]
    skipped_existing: int
    incompatible_existing: int


@dataclass(slots=True)
class Progress:
    selected: int = 0
    indexed: int = 0
    skipped_existing: int = 0
    failed: int = 0
    failed_id: UUID | None = None
    memory_count_before: int | None = None
    memory_count_after: int | None = None
    vector_count_before: int | None = None
    vector_count_after: int | None = None
    memory_unchanged: bool | None = None


class BackfillReader:
    """Every read has its own repeatable-read, read-only transaction."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    @asynccontextmanager
    async def _read_session(self):
        async with self.factory() as session:
            try:
                await session.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                yield session
            finally:
                await session.rollback()

    async def _fingerprints(self, session):
        connection = await session.connection()
        return (await memory_fingerprint(connection), await vector_fingerprint(connection))

    async def fingerprints(self) -> tuple[MemoryFingerprint, VectorFingerprint]:
        async with self._read_session() as session:
            return await self._fingerprints(session)

    async def select(self, *, limit: int, memory_id: UUID | None, reindex: bool) -> Selection:
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise BackfillError("invalid_limit")
        parameters = {
            "memory_id": memory_id, "namespace": MEMORY_VECTOR_NAMESPACE,
            "limit": limit, "reindex": reindex, "model": EMBEDDING_MODEL,
        }
        async with self._read_session() as session:
            fingerprints = await self._fingerprints(session)
            counts = (await session.execute(text("""
                SELECT count(*) FILTER (WHERE v.pk IS NOT NULL) AS existing,
                       count(*) FILTER (WHERE v.pk IS NOT NULL AND NOT
                           (v.metadata_json @> jsonb_build_object(
                               'embedding_provider_id', 'ollama',
                               'embedding_model_tag', CAST(:model AS text),
                               'embedding_dimension', 1024))) AS incompatible
                FROM memories m
                LEFT JOIN kulai_vector_records v
                  ON v.namespace_key = :namespace AND v.record_id = CAST(m.id AS text)
                WHERE (CAST(:memory_id AS uuid) IS NULL OR m.id = CAST(:memory_id AS uuid))
            """), parameters)).mappings().one()
            ids = tuple((await session.execute(text("""
                SELECT m.id FROM memories m
                WHERE (CAST(:memory_id AS uuid) IS NULL OR m.id = CAST(:memory_id AS uuid))
                  AND (:reindex OR NOT EXISTS (
                      SELECT 1 FROM kulai_vector_records v
                      WHERE v.namespace_key = :namespace AND v.record_id = CAST(m.id AS text)
                  ))
                ORDER BY m.created_at ASC, m.id ASC
                LIMIT :limit
            """), parameters)).scalars().all())
            return Selection(
                ids=ids, fingerprints=fingerprints,
                skipped_existing=0 if reindex else counts["existing"],
                incompatible_existing=counts["incompatible"],
            )

    async def read_memory(self, memory_id: UUID, *, expected_fingerprint: MemoryFingerprint):
        async with self._read_session() as session:
            # Hash and canonical row belong to the same repeatable-read snapshot.
            if (await self._fingerprints(session))[0] != expected_fingerprint:
                raise BackfillError("memory_changed")
            return await PostgresMemoryRepository(db=session).get_by_id(memory_id)

    async def has_vector(self, memory_id: UUID) -> bool:
        async with self._read_session() as session:
            return bool(await session.scalar(text("""
                SELECT EXISTS(SELECT 1 FROM kulai_vector_records
                  WHERE namespace_key = :namespace AND record_id = :record_id)
            """), {"namespace": MEMORY_VECTOR_NAMESPACE, "record_id": str(memory_id)}))


async def verify_memory(reader: BackfillReader, expected: MemoryFingerprint) -> None:
    if (await reader.fingerprints())[0] != expected:
        raise BackfillError("memory_changed")


async def execute_batch(
    *, reader: BackfillReader, service: MemoryIndexingService, selection: Selection,
    reindex: bool, progress: Progress,
) -> None:
    """Caller has completed preflight/backup/restore; commit each identity once."""

    progress.selected = len(selection.ids)
    progress.skipped_existing = selection.skipped_existing
    for memory_id in selection.ids:
        try:
            await verify_memory(reader, selection.fingerprints[0])
            if not reindex and await reader.has_vector(memory_id):
                progress.skipped_existing += 1
                continue
            memory = await reader.read_memory(
                memory_id, expected_fingerprint=selection.fingerprints[0]
            )
            if memory is None:
                raise BackfillError("memory_changed")
            # Read session is closed before the provider is invoked.
            request = await service.prepare(memory=memory)
            if request.records[0].metadata["embedding_model_tag"] != EMBEDDING_MODEL:
                raise BackfillError("model_changed")
            await verify_memory(reader, selection.fingerprints[0])
            if not reindex and await reader.has_vector(memory_id):
                progress.skipped_existing += 1
                continue
            await save_prepared_memory_vector(
                service=service, request=request, session_factory=reader.factory
            )
        except BackfillError as exc:
            if exc.code == "model_changed":
                progress.failed += 1
                progress.failed_id = memory_id
            raise
        except Exception:
            progress.failed += 1
            progress.failed_id = memory_id
            raise BackfillError("indexing_failed") from None
        progress.indexed += 1
        await verify_memory(reader, selection.fingerprints[0])
