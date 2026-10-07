from __future__ import annotations

import asyncio
import traceback

import pytest
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_vector_store import VectorRecord, VectorUpsertRequest
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from backend.tests.integration.test_memory_postgres import (
    _owned_migrated_session_factory, _require_opt_in,
)
from kulai_memory import deletion_persistence, indexing_persistence
from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE, Memory, MemoryDeletionError, MemoryDeletionResult,
    MemoryIndexingError, MemoryIndexingService,
)
from kulai_memory.backfill import BackfillReader
from kulai_memory.persistence import PostgresMemoryRepository

PRIVATE = "PRIVATE_DELETE_SQL_CREDENTIALS_SENTINEL 12345.678901"
OTHER_NAMESPACE = "synthetic.other.namespace"


class Provider:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities()

    def __init__(self, engine):
        self.engine = engine
        self.calls = 0

    async def embed(self, request):
        assert self.engine.pool.checkedout() == 0
        assert request.model_hint is None and request.purpose is None
        self.calls += 1
        return EmbeddingResponse(
            provider_id=self.provider_id, model_id="bge-m3:567m-fp16", dimension=1024,
            embeddings=(EmbeddingVector(values=(1.0,) * 1024),),
        )


def _record(memory):
    return VectorRecord(
        id=str(memory.id), vector=EmbeddingVector(values=(1.0,) * 1024),
        metadata={"source_memory_id": str(memory.id), "embedding_provider_id": "ollama",
                  "embedding_model_tag": "bge-m3:567m-fp16", "embedding_dimension": 1024},
    )


async def _seed(factory, *, memory_present=True, vector_present=True):
    memory = Memory(content="synthetic atomic deletion memory")
    async with factory() as session:
        async with session.begin():
            if memory_present:
                await PostgresMemoryRepository(db=session).create(memory)
            if vector_present:
                await PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)).upsert(
                    VectorUpsertRequest(namespace=MEMORY_VECTOR_NAMESPACE, records=(_record(memory),)),
                )
    return memory


async def _state(factory):
    async with factory() as session:
        try:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            memories = tuple((await session.execute(text("SELECT id FROM memories ORDER BY id"))).scalars())
            vectors = tuple((await session.execute(text(
                "SELECT namespace_key, record_id FROM kulai_vector_records ORDER BY namespace_key, record_id"
            ))).all())
            return memories, vectors
        finally:
            await session.rollback()


async def _durable_delete_cases():
    async with _owned_migrated_session_factory() as factory:
        unrelated = await _seed(factory)
        for memory_present, vector_present in ((True, True), (True, False), (False, True), (False, False)):
            memory = await _seed(factory, memory_present=memory_present, vector_present=vector_present)
            async with factory() as session:
                async with session.begin():
                    await PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)).upsert(
                        VectorUpsertRequest(namespace=OTHER_NAMESPACE, records=(_record(memory),)),
                    )
            before_memories, before_vectors = await _state(factory)
            result = await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=factory)
            assert result == MemoryDeletionResult(memory.id, memory_present, int(vector_present))
            after_memories, after_vectors = await _state(factory)
            assert after_memories == tuple(identity for identity in before_memories if identity != memory.id)
            assert after_vectors == tuple(row for row in before_vectors
                                          if row != (MEMORY_VECTOR_NAMESPACE, str(memory.id)))
            assert (OTHER_NAMESPACE, str(memory.id)) in after_vectors
            assert unrelated.id in after_memories
            assert await deletion_persistence.delete_memory(
                memory_id=memory.id, session_factory=factory,
            ) == MemoryDeletionResult(memory.id, False, 0)
            assert await _state(factory) == (after_memories, after_vectors)
        print("atomic.delete.durable_cases=4;repeat=PASS;namespace_isolation=PASS")


async def _rollback_failure(stage, monkeypatch):
    async with _owned_migrated_session_factory() as factory:
        memory = await _seed(factory)
        reader = BackfillReader(factory)
        before = await reader.fingerprints()
        calls = []

        class Repository(PostgresMemoryRepository):
            async def delete_by_id(self, identity):
                result = await super().delete_by_id(identity)
                calls.append("memory")
                if stage == "memory":
                    raise RuntimeError(PRIVATE)
                return result

        class Store(PgVectorStore):
            async def delete(self, request):
                result = await super().delete(request)
                calls.append("vector")
                if stage == "vector":
                    raise RuntimeError(PRIVATE)
                if stage == "cancel":
                    raise asyncio.CancelledError()
                return result

        monkeypatch.setattr(deletion_persistence, "PostgresMemoryRepository", Repository)
        monkeypatch.setattr(deletion_persistence, "PgVectorStore", Store)

        class RejectCommitSession(Session):
            pass

        def fail_commit(session):
            calls.append("commit")
            raise RuntimeError(PRIVATE)

        if stage == "commit":
            event.listen(RejectCommitSession, "before_commit", fail_commit)
            deleting_factory = async_sessionmaker(factory.kw["bind"], sync_session_class=RejectCommitSession)
        else:
            deleting_factory = factory
        try:
            error = asyncio.CancelledError if stage == "cancel" else MemoryDeletionError
            with pytest.raises(error) as caught:
                await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=deleting_factory)
            public = "".join(traceback.format_exception(caught.value))
            assert PRIVATE not in public and "12345.678901" not in public
        finally:
            if stage == "commit":
                event.remove(RejectCommitSession, "before_commit", fail_commit)
        assert calls == (["memory"] if stage == "memory" else
                         ["memory", "vector", "commit"] if stage == "commit" else ["memory", "vector"])
        assert await reader.fingerprints() == before
        memories, vectors = await _state(factory)
        assert memories == (memory.id,) and vectors == ((MEMORY_VECTOR_NAMESPACE, str(memory.id)),)
        print(f"atomic.delete.rollback_stage={stage};fingerprints_unchanged=true")


async def _concurrent_duplicate_delete():
    async with _owned_migrated_session_factory() as factory:
        memory = await _seed(factory)
        results = await asyncio.wait_for(asyncio.gather(
            deletion_persistence.delete_memory(memory_id=memory.id, session_factory=factory),
            deletion_persistence.delete_memory(memory_id=memory.id, session_factory=factory),
        ), timeout=15)
        assert {(result.memory_deleted, result.vector_deleted_count) for result in results} == {(True, 1), (False, 0)}
        assert all(result.memory_id == memory.id for result in results)
        assert await _state(factory) == ((), ())
        print("atomic.delete.concurrent_duplicate=PASS;memory_count=0;vector_count=0")


async def _wait_for_lock(engine, pid):
    async def poll():
        async with engine.connect() as connection:
            while True:
                waiting = await connection.scalar(text(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                ), {"pid": pid})
                if waiting == "Lock":
                    return
                # Refresh statistics on each observation in the same connection.
                await connection.rollback()
                await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout=5)


async def _delete_index_race(winner, monkeypatch):
    async with _owned_migrated_session_factory() as factory:
        engine = factory.kw["bind"]
        memory = await _seed(factory)
        provider = Provider(engine)
        service = MemoryIndexingService(provider=provider, expected_dimension=1024)
        prepared = await service.prepare(memory=memory)
        held, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observation = {}
        writes = []

        class IndexStore(PgVectorStore):
            async def upsert(self, request):
                # Host's canonical row lock has already been acquired.
                if winner == "index":
                    held.set()
                    await release.wait()
                writes.append("upsert")
                return await super().upsert(request)

        class DeleteStore(PgVectorStore):
            async def delete(self, request):
                # The canonical DELETE is already executed but uncommitted.
                if winner == "delete":
                    held.set()
                    await release.wait()
                return await super().delete(request)

        class WaitingSession(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                if winner == "index" and "pid" not in observation:
                    result = await super().execute(text("SELECT pg_backend_pid()"))
                    observation["pid"] = result.scalar_one()
                    attempting.set()
                return await super().execute(statement, *args, **kwargs)

            async def scalar(self, statement, *args, **kwargs):
                if winner == "delete" and "pid" not in observation:
                    observation["pid"] = await super().scalar(text("SELECT pg_backend_pid()"))
                    attempting.set()
                return await super().scalar(statement, *args, **kwargs)

        monkeypatch.setattr(indexing_persistence, "PgVectorStore", IndexStore)
        monkeypatch.setattr(deletion_persistence, "PgVectorStore", DeleteStore)
        waiting_factory = async_sessionmaker(engine, class_=WaitingSession)

        async def index(target):
            return await indexing_persistence.save_prepared_memory_vector(
                service=service, request=prepared, session_factory=target,
            )

        async def delete(target):
            return await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=target)

        tasks = []
        try:
            first_operation, second_operation = (index, delete) if winner == "index" else (delete, index)
            tasks.append(asyncio.create_task(first_operation(factory)))
            await asyncio.wait_for(held.wait(), timeout=5)
            tasks.append(asyncio.create_task(second_operation(waiting_factory)))
            await asyncio.wait_for(attempting.wait(), timeout=5)
            await _wait_for_lock(engine, observation["pid"])
            assert not tasks[1].done()
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
        if winner == "index":
            assert results[0].ids == (str(memory.id),)
            assert results[1] == MemoryDeletionResult(memory.id, True, 1)
            assert writes == ["upsert"]
        else:
            assert results[0] == MemoryDeletionResult(memory.id, True, 1)
            assert isinstance(results[1], MemoryIndexingError)
            assert writes == []
        assert provider.calls == 1
        assert await _state(factory) == ((), ())
        print(f"atomic.delete.index_race.winner={winner};observed_pg_lock=true;memory_count=0;vector_count=0")


async def _stale_prepared_request(monkeypatch):
    async with _owned_migrated_session_factory() as factory:
        memory = await _seed(factory)
        service = MemoryIndexingService(provider=Provider(factory.kw["bind"]), expected_dimension=1024)
        prepared = await service.prepare(memory=memory)
        await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=factory)
        calls = []

        def unexpected_store(**kwargs):
            calls.append("store")
            raise AssertionError("Deleted Memory must never be reindexed")

        monkeypatch.setattr(indexing_persistence, "PgVectorStore", unexpected_store)
        with pytest.raises(MemoryIndexingError):
            await indexing_persistence.save_prepared_memory_vector(
                service=service, request=prepared, session_factory=factory,
            )
        assert calls == []
        assert await _state(factory) == ((), ())


def test_real_atomic_durable_delete_missing_and_namespace_isolation():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_durable_delete_cases(), timeout=90))


@pytest.mark.parametrize("stage", ["memory", "vector", "commit", "cancel"])
def test_real_delete_failures_roll_back_both_records(stage, monkeypatch):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_rollback_failure(stage, monkeypatch), timeout=90))


def test_real_concurrent_duplicate_delete():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_concurrent_duplicate_delete(), timeout=90))


@pytest.mark.parametrize("winner", ["index", "delete"])
def test_real_delete_index_race_uses_canonical_row_lock(winner, monkeypatch):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_delete_index_race(winner, monkeypatch), timeout=90))


def test_real_stale_prepared_request_cannot_recreate_vector(monkeypatch):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_stale_prepared_request(monkeypatch), timeout=90))
