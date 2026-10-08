"""Crash recovery proofs: all data and writes belong to guarded temporary DBs."""
from __future__ import annotations

import asyncio
import os
import traceback
from uuid import uuid4

import pytest
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_transcription import TranscriptionResult
from kulai_provider_ollama_embeddings import provider as ollama_module
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.test_desktop_controller import FakeProvider as SttProvider, FakeRecorder
from kulai_memory.application import MemoryService, MemoryIndexingService, TranscriptMemoryIngestionStatus
from kulai_memory.application.indexing import MemoryIndexingError, MemoryIndexingIncompatibleError, MemoryIndexingMissingError, MemoryIndexingState
from kulai_memory.application.retrieval import MemoryRetrievalService
from kulai_memory.automatic_indexing import AutomaticMemoryIndexer
from kulai_memory.database_safety import database_config, database_config_for_database, memory_fingerprint
from kulai_memory.deletion_persistence import delete_memory
from kulai_memory.desktop.controller import DesktopController
from kulai_memory.desktop.models import DesktopResultStatus
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.indexing_persistence import ensure_memory_indexed, index_memory
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.retrieval_persistence import retrieve_memories
from kulai_memory.server import VoiceMemoryServerRuntime, ServerIndexingError
from kulai_memory.settings import Settings

MODEL = "bge-m3:567m-fp16"


class CheckedProvider:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities()

    def __init__(self, engine):
        self.engine = engine
        self.calls = 0
        self.closed = 0
        self.error = None
        self.before = None
        self.require_no_checkout = True

    async def embed(self, request):
        if self.require_no_checkout:
            assert self.engine.pool.checkedout() == 0
        assert request.model_hint is request.purpose is None
        self.calls += 1
        if self.before is not None:
            await self.before()
        if self.error is not None:
            raise self.error
        return EmbeddingResponse(provider_id="ollama", model_id=MODEL, dimension=1024,
            embeddings=(EmbeddingVector(values=(1.0,) + (0.0,) * 1023),))

    async def aclose(self): self.closed += 1


async def create(factory, content="synthetic automatic indexing memory"):
    async with factory() as session:
        memory = await MemoryService(repository=PostgresMemoryRepository(db=session)).create_memory(content=content)
        await session.commit()
    return memory


async def counts(factory):
    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        result = []
        for table in ("memories", "kulai_vector_records", "memory_ingestion_tombstones"):
            result.append(int(await session.scalar(text(f"SELECT count(*) FROM {table}"))))
        await session.rollback()
        return tuple(result)


async def memory_hash(factory):
    async with factory.kw["bind"].connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        result = await memory_fingerprint(connection)
        await connection.rollback()
        return result


def indexer(factory, provider):
    return AutomaticMemoryIndexer(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
        session_factory=factory, provider_factory=lambda **kwargs: provider)


async def compatibility(factory, memory, *, field=None):
    provider = CheckedProvider(factory.kw["bind"])
    await index_memory(memory=memory, service=MemoryIndexingService(provider=provider, expected_dimension=1024), session_factory=factory)
    if field:
        async with factory() as session:
            await session.execute(text("UPDATE kulai_vector_records SET metadata_json = metadata_json - :field WHERE record_id=:id"),
                                  {"field": field, "id": str(memory.id)})
            await session.commit()


@pytest.mark.parametrize("failure", ["embedding", "upsert", "commit", "cancellation"])
def test_durable_memory_survives_index_failure_then_duplicate_repair(monkeypatch, failure):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            from kulai_memory import indexing_persistence as adapter
            memory = await create(factory)
            before = await memory_hash(factory)
            provider = CheckedProvider(factory.kw["bind"])
            service = MemoryIndexingService(provider=provider, expected_dimension=1024)
            write_factory = factory
            if failure == "embedding": provider.error = RuntimeError("PRIVATE_EMBED_SENTINEL")
            if failure == "cancellation": provider.error = asyncio.CancelledError()
            if failure == "upsert":
                original = adapter.PgVectorStore
                class BrokenStore(original):
                    async def upsert(self, request):
                        await super().upsert(request)
                        raise RuntimeError("PRIVATE_VECTOR_SENTINEL")
                monkeypatch.setattr(adapter, "PgVectorStore", BrokenStore)
            if failure == "commit":
                class RejectCommit(Session): pass
                def fail(session): raise RuntimeError("PRIVATE_COMMIT_SENTINEL")
                event.listen(RejectCommit, "before_commit", fail)
                write_factory = async_sessionmaker(factory.kw["bind"], sync_session_class=RejectCommit, expire_on_commit=False)
            try:
                with pytest.raises(asyncio.CancelledError if failure == "cancellation" else MemoryIndexingError) as caught:
                    await ensure_memory_indexed(memory=memory, service=service, session_factory=write_factory)
                assert "PRIVATE_" not in "".join(traceback.format_exception(caught.value))
            finally:
                if failure == "commit": event.remove(RejectCommit, "before_commit", fail)
            assert await counts(factory) == (1, 0, 0)
            assert await memory_hash(factory) == before
            provider.error = None
            if failure == "upsert": monkeypatch.setattr(adapter, "PgVectorStore", original)
            async with factory() as session:
                write = await PostgresMemoryRepository(db=session).create_or_get_by_ingestion_id(memory)
                assert not write.created and write.memory.id == memory.id
                await session.commit()
            repaired = await ensure_memory_indexed(memory=write.memory, service=service, session_factory=factory)
            assert repaired.state is MemoryIndexingState.INDEXED
            calls = provider.calls
            assert (await ensure_memory_indexed(memory=memory, service=service, session_factory=factory)).state is MemoryIndexingState.ALREADY_INDEXED
            assert provider.calls == calls and await counts(factory) == (1, 1, 0)
            assert await memory_hash(factory) == before
    asyncio.run(scenario())


def test_startup_bounded_order_and_incompatible_no_overwrite():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            a, b, c, d = [await create(factory, f"synthetic note {name}") for name in "ABCD"]
            await compatibility(factory, a)
            await compatibility(factory, c, field="source_memory_id")
            before = await memory_hash(factory)
            provider = CheckedProvider(factory.kw["bind"])
            worker = indexer(factory, provider)
            report = await worker.reconcile(limit=1)
            assert report.selected == report.indexed == report.remaining_missing == report.incompatible_existing == 1
            assert report.compatible_existing == 2 and report.degraded
            async with factory() as session:
                ids = set((await session.execute(text("SELECT record_id FROM kulai_vector_records"))).scalars())
            assert str(b.id) in ids and str(d.id) not in ids
            report = await worker.reconcile(limit=100)
            assert report.indexed == 1 and report.remaining_missing == 0 and report.incompatible_existing == 1
            with pytest.raises(MemoryIndexingIncompatibleError): await worker.ensure(memory=c)
            assert provider.calls == 2 and await counts(factory) == (4, 4, 0)
            async with factory() as session:
                value = await session.scalar(text("SELECT metadata_json FROM kulai_vector_records WHERE record_id=:id"), {"id": str(c.id)})
            assert "source_memory_id" not in value
            assert await memory_hash(factory) == before
            await worker.aclose()
            assert provider.closed == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("other", ["compatible", "incompatible", "delete"])
def test_second_check_rejects_stale_embedding_after_concurrent_change(other):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            entered, release = asyncio.Event(), asyncio.Event()
            provider = CheckedProvider(factory.kw["bind"])
            async def block():
                entered.set()
                await release.wait()
            provider.before = block
            service = MemoryIndexingService(provider=provider, expected_dimension=1024)
            operation = asyncio.create_task(ensure_memory_indexed(memory=memory, service=service, session_factory=factory))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                if other == "delete":
                    await delete_memory(memory_id=memory.id, session_factory=factory)
                else:
                    await compatibility(factory, memory, field="source_memory_id" if other == "incompatible" else None)
                async with factory() as session:
                    original = tuple((await session.execute(text("SELECT pk, updated_at, metadata_json FROM kulai_vector_records"))).all())
                release.set()
                if other == "compatible":
                    assert (await asyncio.wait_for(operation, 10)).state is MemoryIndexingState.ALREADY_INDEXED
                else:
                    with pytest.raises(MemoryIndexingMissingError if other == "delete" else MemoryIndexingIncompatibleError):
                        await asyncio.wait_for(operation, 10)
                async with factory() as session:
                    after = tuple((await session.execute(text("SELECT pk, updated_at, metadata_json FROM kulai_vector_records"))).all())
                assert original == after
                assert await counts(factory) == ((0, 0, 1) if other == "delete" else (1, 1, 0))
            finally:
                release.set()
                await asyncio.gather(operation, return_exceptions=True)
    asyncio.run(scenario())


def runtime_arguments(factory, provider):
    engine = factory.kw["bind"]
    return dict(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
        provider_factory=lambda ignored: SttProvider(("synthetic automatically indexed speech",)),
        database_config_factory=lambda: database_config_for_database(engine.url.database, config=database_config()),
        engine_factory=lambda ignored: engine, session_factory_builder=lambda ignored: factory,
        indexer_factory=lambda **kwargs: AutomaticMemoryIndexer(**kwargs, provider_factory=lambda **ignored: provider))


def test_two_automatic_workers_wait_on_canonical_lock_and_only_one_upserts(monkeypatch):
    _require_opt_in()
    async def scenario():
        from kulai_memory import indexing_persistence as adapter
        from backend.tests.integration.test_memory_deletion_postgres import _wait_for_lock
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            before = await memory_hash(factory)
            embeds_ready, release_embeds, writing, release_write = (asyncio.Event() for _ in range(4))
            original_store, original_lock = adapter.PgVectorStore, adapter._lock_memory_for_indexing
            pids, writes, embeds, writer_pids = [], [], [], []
            class WaitingStore(original_store):
                async def upsert(self, request):
                    writer_pids.append(await self._db.scalar(text("SELECT pg_backend_pid()")))
                    writes.append(request.records[0].id)
                    result = await super().upsert(request)
                    writing.set()
                    await release_write.wait()
                    return result
            async def lock(**kwargs):
                pids.append(await kwargs["db"].scalar(text("SELECT pg_backend_pid()")))
                await original_lock(**kwargs)
            async def both_ready():
                embeds.append(True)
                if len(embeds) == 2:
                    assert factory.kw["bind"].pool.checkedout() == 0
                    embeds_ready.set()
                await release_embeds.wait()
            monkeypatch.setattr(adapter, "PgVectorStore", WaitingStore)
            monkeypatch.setattr(adapter, "_lock_memory_for_indexing", lock)
            providers = [CheckedProvider(factory.kw["bind"]) for _ in range(2)]
            for provider in providers:
                provider.before = both_ready
                # Another worker may still be completing its short pre-check.
                provider.require_no_checkout = False
            tasks = [asyncio.create_task(ensure_memory_indexed(memory=memory,
                service=MemoryIndexingService(provider=provider, expected_dimension=1024),
                session_factory=factory)) for provider in providers]
            try:
                await asyncio.wait_for(embeds_ready.wait(), 10)
                release_embeds.set()
                await asyncio.wait_for(writing.wait(), 10)
                async with asyncio.timeout(10):
                    while len(pids) != 2: await asyncio.sleep(0.01)
                    await _wait_for_lock(factory.kw["bind"], next(pid for pid in pids if pid != writer_pids[0]))
                release_write.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
                assert {r.state for r in results} == {MemoryIndexingState.INDEXED, MemoryIndexingState.ALREADY_INDEXED}
                assert writes == [str(memory.id)] and await counts(factory) == (1, 1, 0)
                assert await memory_hash(factory) == before
            finally:
                release_embeds.set()
                release_write.set()
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_startup_index_failure_is_degraded_then_bounded_repair_works():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            before = await memory_hash(factory)
            provider = CheckedProvider(factory.kw["bind"])
            provider.error = RuntimeError("PRIVATE_STARTUP_EMBED_SENTINEL")
            runtime = VoiceMemoryServerRuntime(**runtime_arguments(factory, provider))
            try:
                await runtime.startup()
                assert runtime.started and runtime.indexing_report.degraded
                assert runtime.indexing_report.failed == runtime.indexing_report.remaining_missing == 1
                assert await counts(factory) == (1, 0, 0)
                provider.error = None
                assert (await runtime.reconcile_missing_indexes()).indexed == 1
                assert not runtime.indexing_report.degraded and await counts(factory) == (1, 1, 0)
                assert await memory_hash(factory) == before
            finally: await runtime.shutdown()
            assert provider.closed == 1
    asyncio.run(scenario())


def test_server_startup_repairs_crash_gap_and_index_failure_retry():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            crash_memory = await create(factory)
            provider = CheckedProvider(factory.kw["bind"])
            runtime = VoiceMemoryServerRuntime(**runtime_arguments(factory, provider))
            try:
                await runtime.startup()
                assert runtime.indexing_report.indexed == 1
                ingestion_id, session_id = uuid4(), uuid4()
                transcript = TranscriptionResult(text="synthetic successful note", provider_id="fake")
                provider.error = RuntimeError("PRIVATE_INDEX_FAILURE")
                with pytest.raises(ServerIndexingError) as caught:
                    await runtime.ingest(transcription=transcript, ingestion_id=ingestion_id, session_id=session_id)
                assert await counts(factory) == (2, 1, 0)
                provider.error = None
                repaired = await runtime.ingest(transcription=transcript, ingestion_id=ingestion_id, session_id=session_id)
                assert repaired.status is TranscriptMemoryIngestionStatus.DUPLICATE
                assert repaired.memory.id == caught.value.memory_id
                assert await counts(factory) == (2, 2, 0)
                previous = provider.calls
                await runtime.ingest(transcription=transcript, ingestion_id=ingestion_id, session_id=session_id)
                assert provider.calls == previous
                assert not (await runtime.reconcile_missing_indexes()).degraded
            finally: await runtime.shutdown()
            assert provider.closed == 1
    asyncio.run(scenario())


def test_desktop_real_commit_then_index_failure_retry_and_reuse(tmp_path):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            provider = CheckedProvider(factory.kw["bind"])
            stt = SttProvider(("synthetic desktop note", "second synthetic desktop note"))
            arguments = runtime_arguments(factory, provider)
            arguments["provider_factory"] = lambda ignored: stt
            controller = DesktopController(**arguments, recorder=FakeRecorder(tmp_path))
            try:
                await controller.startup()
                ingestion_id = await controller.start_recording(device_id=3)
                provider.error = RuntimeError("PRIVATE_INDEX_FAILURE")
                result = await controller.stop_and_process()
                assert result.status is DesktopResultStatus.INDEXING_FAILED and result.memory_id
                assert await counts(factory) == (1, 0, 0)
                provider.error = None
                retry = await controller.retry_save()
                assert retry.status is DesktopResultStatus.DUPLICATE and retry.memory_id == result.memory_id
                assert len(stt.requests) == 1 and await counts(factory) == (1, 1, 0)
                assert (await controller.list_recent())[0].id == result.memory_id
                await controller.start_recording(device_id=3)
                assert (await controller.stop_and_process()).status is DesktopResultStatus.CREATED
                assert await counts(factory) == (2, 2, 0)
            finally: await controller.shutdown()
            assert provider.closed == 1
    asyncio.run(scenario())


def test_real_bge_automatic_index_recovery_and_retrieval(monkeypatch):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_OLLAMA_INTEGRATION") != "1": pytest.skip("Enable local Ollama integration.")
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw["bind"]
            original = ollama_module.AsyncClient
            counters = dict(clients=0, requests=0, closed=0, native_1024=0, overrides=0)
            class Client(original):
                def __init__(self, *args, **kwargs):
                    counters["clients"] += 1
                    super().__init__(*args, **kwargs)
                async def embed(self, *args, **kwargs):
                    assert engine.pool.checkedout() == 0
                    counters["requests"] += 1
                    counters["overrides"] += int(kwargs.get("dimensions") is not None)
                    result = await super().embed(*args, **kwargs)
                    assert len(result.embeddings) == 1 and len(result.embeddings[0]) == 1024
                    counters["native_1024"] += 1
                    return result
                async def close(self):
                    counters["closed"] += 1
                    await super().close()
            monkeypatch.setattr(ollama_module, "AsyncClient", Client)
            # A new runtime discovers a durable Memory committed by the old process.
            crash = await create(factory, "Android voice notes use WebSocket and ADB reverse.")
            provider = create_embedding_provider(settings=Settings(_env_file=None, kulai_vector_dimension=1024))
            runtime = VoiceMemoryServerRuntime(**runtime_arguments(factory, provider))
            try:
                await runtime.startup()
                assert runtime.indexing_report.indexed == 1
                identities = [(uuid4(), uuid4()) for _ in range(2)]
                results = []
                for content, (ingestion_id, session_id) in zip(
                    ("Tomato soup needs basil and garlic.", "Jalapeno peppers grow in a hydroponic bucket."), identities):
                    results.append(await runtime.ingest(transcription=TranscriptionResult(text=content, provider_id="fake"),
                        ingestion_id=ingestion_id, session_id=session_id))
                assert all(result.status is TranscriptMemoryIngestionStatus.CREATED for result in results)
                duplicate = await runtime.ingest(transcription=TranscriptionResult(text=results[0].memory.content, provider_id="fake"),
                    ingestion_id=identities[0][0], session_id=identities[0][1])
                assert duplicate.memory.id == results[0].memory.id and counters["requests"] == 3
                before = await memory_hash(factory)
                retrieval = await retrieve_memories(query="Which herbs are needed for tomato soup?", top_k=1,
                    service=MemoryRetrievalService(provider=provider, expected_dimension=1024,
                        expected_provider_id="ollama", expected_model_tag=MODEL), session_factory=factory)
                assert retrieval.hits[0].memory.id == results[0].memory.id
                assert await memory_hash(factory) == before and await counts(factory) == (3, 3, 0)
            finally: await runtime.shutdown()
            assert counters == dict(clients=1, requests=4, closed=1, native_1024=4, overrides=0)
            print("automatic_bge=" + str(counters) + "; checkout_during_embed=0; retrieval=PASS")
    asyncio.run(scenario())
