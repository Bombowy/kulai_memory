"""Lifecycle and concurrency proofs exclusively on owned temporary databases."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in, _upgrade_database
from backend.tests.integration.test_automatic_indexing_postgres import CheckedProvider, create, counts, indexer
from kulai_memory.application import MemoryService, MemoryArchivedError, MemoryIdempotencyConflictError
from kulai_memory.application.indexing import MemoryIndexingService, MemoryIndexingArchivedError, MemoryIndexingStaleRevisionError
from kulai_memory.application.lifecycle import MemoryLifecycleService, MemoryLifecycleError, MemoryRevisionConflictError
from kulai_memory.application.retrieval import MemoryRetrievalService, MemoryRetrievalError
from kulai_memory.database_safety import (
    database_config, async_database_url, database_snapshot_url, create_owned_temporary_database,
    drop_owned_temporary_database, run_database_doctor,
)
from kulai_memory.deletion_persistence import delete_memory
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.indexing_persistence import save_prepared_memory_vector
from kulai_memory.lifecycle_persistence import change_memory
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.retrieval_persistence import retrieve_memories
from kulai_memory.settings import Settings
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig


async def read(factory, memory_id):
    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        result = await PostgresMemoryRepository(db=session).get_by_id(memory_id)
        await session.rollback()
        return result


async def snapshot(factory):
    return await database_snapshot_url(str(factory.kw["bind"].url.render_as_string(hide_password=False)))


async def mutate_only(factory, action, memory, **kwargs):
    async with factory() as session:
        async with session.begin():
            service = MemoryLifecycleService(repository=PostgresMemoryRepository(db=session),
                store=PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)))
            return await getattr(service, action)(memory_id=memory.id, **kwargs)


async def lifecycle(factory, provider):
    worker = indexer(factory, provider)
    memory = await create(factory, "Android voice client uses WebSocket.")
    try:
        await worker.ensure(memory=memory)
        edited = await change_memory(action="edit", memory_id=memory.id, content="Tomato soup needs basil and garlic.",
            expected_revision=1, session_factory=factory, indexer=worker)
        assert edited.canonical.memory.revision == 2 and not edited.indexing_error_code
        assert await counts(factory) == (1, 1, 0)
        async with factory() as session:
            row = (await session.execute(text("SELECT record_id, metadata_json, vector_dims(embedding) FROM kulai_vector_records"))).one()
            assert row[0] == str(memory.id) and row[1]["revision"] == 2 and row[2] == 1024
        with pytest.raises(MemoryRevisionConflictError):
            await change_memory(action="edit", memory_id=memory.id, content="stale edit", expected_revision=1, session_factory=factory, indexer=worker)
        async with factory() as session:
            with pytest.raises(MemoryIdempotencyConflictError):
                await MemoryService(repository=PostgresMemoryRepository(db=session)).create_memory_idempotent(
                    ingestion_id=memory.ingestion_id, content=memory.content)
        archived = await change_memory(action="archive", memory_id=memory.id, session_factory=factory)
        assert archived.canonical.memory.archived_at is not None and archived.canonical.memory.revision == 2
        assert await counts(factory) == (1, 0, 0)
        before = await snapshot(factory)
        await change_memory(action="archive", memory_id=memory.id, session_factory=factory)
        assert await snapshot(factory) == before
        assert not (await worker.reconcile()).degraded
        async with factory() as session:
            assert await PostgresMemoryRepository(db=session).list_recent(limit=20) == ()
            assert len(await PostgresMemoryRepository(db=session).list_recent(limit=20, include_archived=True)) == 1
            with pytest.raises(MemoryArchivedError):
                await MemoryService(repository=PostgresMemoryRepository(db=session)).create_memory_idempotent(
                    ingestion_id=memory.ingestion_id, content=edited.canonical.memory.content)
        with pytest.raises(MemoryArchivedError):
            await change_memory(action="edit", memory_id=memory.id, content="invalid archive edit", expected_revision=2, session_factory=factory, indexer=worker)
        restored = await change_memory(action="restore", memory_id=memory.id, session_factory=factory, indexer=worker)
        assert restored.canonical.memory.archived_at is None and restored.canonical.memory.revision == 2
        assert await counts(factory) == (1, 1, 0)
        await change_memory(action="restore", memory_id=memory.id, session_factory=factory, indexer=worker)
        service = MemoryRetrievalService(provider=provider, expected_dimension=1024,
            expected_provider_id="ollama", expected_model_tag="bge-m3:567m-fp16")
        result = await retrieve_memories(query="Which herbs are used in tomato soup?", top_k=1, service=service, session_factory=factory)
        assert result.hits[0].memory.id == memory.id and result.hits[0].memory.revision == 2
        archived = await change_memory(action="archive", memory_id=memory.id, session_factory=factory)
        assert (await retrieve_memories(query="Which herbs are used in tomato soup?", top_k=1, service=service, session_factory=factory)).hits == ()
        await delete_memory(memory_id=memory.id, session_factory=factory)
        assert await counts(factory) == (0, 0, 1)
    finally:
        await worker.aclose()


def test_real_postgres_edit_archive_restore_and_retrieval():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            provider = CheckedProvider(factory.kw["bind"])
            await lifecycle(factory, provider)
            assert provider.calls == 5 and provider.closed == 1
    asyncio.run(asyncio.wait_for(scenario(), 90))


def test_real_bge_edit_archive_restore_native_1024_and_client_reuse(monkeypatch):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_OLLAMA_INTEGRATION") != "1": pytest.skip("Enable local Ollama integration.")
    from kulai_provider_ollama_embeddings import provider as module
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            counters = dict(clients=0, requests=0, native_1024=0, overrides=0, closed=0)
            original = module.AsyncClient
            class Client(original):
                def __init__(self, *args, **kwargs):
                    counters["clients"] += 1
                    super().__init__(*args, **kwargs)
                async def embed(self, *args, **kwargs):
                    assert factory.kw["bind"].pool.checkedout() == 0
                    counters["requests"] += 1
                    counters["overrides"] += int(kwargs.get("dimensions") is not None)
                    response = await super().embed(*args, **kwargs)
                    assert len(response.embeddings[0]) == 1024
                    counters["native_1024"] += 1
                    return response
                async def close(self):
                    counters["closed"] += 1
                    await super().close()
            monkeypatch.setattr(module, "AsyncClient", Client)
            provider = create_embedding_provider(settings=Settings(_env_file=None, kulai_vector_dimension=1024))
            await lifecycle(factory, provider)
            assert counters == dict(clients=1, requests=5, native_1024=5, overrides=0, closed=1)
            print("lifecycle_bge=" + str(counters) + ";checkout_during_embed=0;retrieval=PASS")
    asyncio.run(asyncio.wait_for(scenario(), 180))


@pytest.mark.parametrize("action", ["edit", "restore"])
def test_index_failure_preserves_committed_change_and_reconciliation_repairs(action):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            provider = CheckedProvider(factory.kw["bind"])
            worker = indexer(factory, provider)
            try:
                memory = await create(factory)
                await worker.ensure(memory=memory)
                if action == "restore": await mutate_only(factory, "archive", memory)
                provider.error = RuntimeError("PRIVATE_LIFECYCLE_OLLAMA_SENTINEL")
                result = await change_memory(action=action, memory_id=memory.id, session_factory=factory, indexer=worker,
                    content="edited durable content", expected_revision=1)
                assert result.indexing_error_code and await counts(factory) == (1, 0, 0)
                canonical = await read(factory, memory.id)
                assert canonical.archived_at is None and canonical.revision == (2 if action == "edit" else 1)
                before = (await snapshot(factory)).memories
                provider.error = None
                assert (await worker.reconcile()).indexed == 1
                assert (await snapshot(factory)).memories == before and await counts(factory) == (1, 1, 0)
            finally: await worker.aclose()
    asyncio.run(asyncio.wait_for(scenario(), 90))


@pytest.mark.parametrize("action", ["edit", "archive", "delete"])
def test_prepared_embedding_before_mutation_cannot_recreate_stale_vector(action):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            provider = CheckedProvider(factory.kw["bind"])
            service = MemoryIndexingService(provider=provider, expected_dimension=1024)
            entered, release = asyncio.Event(), asyncio.Event()
            async def block(): entered.set(); await release.wait()
            provider.before = block
            task = asyncio.create_task(service.prepare(memory=memory))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                if action == "delete": await delete_memory(memory_id=memory.id, session_factory=factory)
                else: await mutate_only(factory, action, memory, **(dict(content="new revision", expected_revision=1) if action == "edit" else {}))
                release.set()
                prepared = await asyncio.wait_for(task, 5)
                before = await snapshot(factory)
                from kulai_memory.application.indexing import MemoryIndexingMissingError
                expected = dict(edit=MemoryIndexingStaleRevisionError, archive=MemoryIndexingArchivedError, delete=MemoryIndexingMissingError)[action]
                with pytest.raises(expected):
                    await save_prepared_memory_vector(service=service, request=prepared, session_factory=factory)
                assert await snapshot(factory) == before and before.vectors.count == 0
            finally:
                release.set()
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(asyncio.wait_for(scenario(), 90))


@pytest.mark.parametrize("failure", ["vector", "commit", "cancel"])
def test_atomic_mutation_failure_rolls_back_memory_and_vector(monkeypatch, failure):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            provider = CheckedProvider(factory.kw["bind"])
            worker = indexer(factory, provider)
            try:
                await worker.ensure(memory=memory)
                before = await snapshot(factory)
                write_factory = factory
                if failure == "commit":
                    class RejectCommit(Session): pass
                    def reject(session): raise RuntimeError("PRIVATE_COMMIT")
                    event.listen(RejectCommit, "before_commit", reject)
                    write_factory = async_sessionmaker(factory.kw["bind"], sync_session_class=RejectCommit, expire_on_commit=False)
                else:
                    from kulai_memory import lifecycle_persistence as adapter
                    original = adapter.PgVectorStore
                    class Broken(original):
                        async def delete(self, request):
                            await super().delete(request)
                            if failure == "cancel": raise asyncio.CancelledError()
                            raise RuntimeError("PRIVATE_VECTOR")
                    monkeypatch.setattr(adapter, "PgVectorStore", Broken)
                try:
                    with pytest.raises(asyncio.CancelledError if failure == "cancel" else MemoryLifecycleError):
                        await change_memory(action="archive", memory_id=memory.id, session_factory=write_factory)
                finally:
                    if failure == "commit": event.remove(RejectCommit, "before_commit", reject)
                assert await snapshot(factory) == before
            finally: await worker.aclose()
    asyncio.run(asyncio.wait_for(scenario(), 90))


def test_two_concurrent_edits_one_revision_conflict_no_deadlock():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            async def edit(content):
                return await mutate_only(factory, "edit", memory, content=content, expected_revision=1)
            results = await asyncio.wait_for(asyncio.gather(edit("edit A"), edit("edit B"), return_exceptions=True), 10)
            assert sum(isinstance(r, MemoryRevisionConflictError) for r in results) == 1
            current = await read(factory, memory.id)
            assert current.revision == 2 and current.content in {"edit A", "edit B"}
            assert await counts(factory) == (1, 0, 0)
    asyncio.run(asyncio.wait_for(scenario(), 90))


@pytest.mark.parametrize("action", ["edit", "archive"])
def test_index_wins_then_mutation_waits_on_real_postgres_lock_and_removes_vector(action, monkeypatch):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            from kulai_memory import indexing_persistence as adapter
            from backend.tests.integration.test_memory_deletion_postgres import _wait_for_lock
            memory = await create(factory)
            service = MemoryIndexingService(provider=CheckedProvider(factory.kw["bind"]), expected_dimension=1024)
            prepared = await service.prepare(memory=memory)
            held, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
            observation = {}
            original = adapter._lock_memory_for_indexing
            async def hold(**kwargs):
                await original(**kwargs)
                held.set()
                await release.wait()
            monkeypatch.setattr(adapter, "_lock_memory_for_indexing", hold)
            class WaitSession(AsyncSession):
                async def scalar(self, statement, *args, **kwargs):
                    if "FOR UPDATE" in str(statement):
                        observation["pid"] = await super().scalar(text("SELECT pg_backend_pid()"))
                        attempting.set()
                    return await super().scalar(statement, *args, **kwargs)
            waiting_factory = async_sessionmaker(factory.kw["bind"], class_=WaitSession, expire_on_commit=False)
            writer = asyncio.create_task(save_prepared_memory_vector(service=service, request=prepared, session_factory=factory))
            mutator = None
            try:
                await asyncio.wait_for(held.wait(), 5)
                mutator = asyncio.create_task(mutate_only(waiting_factory, action, memory,
                    **(dict(content="new revision", expected_revision=1) if action == "edit" else {})))
                await asyncio.wait_for(attempting.wait(), 5)
                await _wait_for_lock(factory.kw["bind"], observation["pid"])
                assert not mutator.done()
                release.set()
                await asyncio.wait_for(asyncio.gather(writer, mutator), 10)
                assert await counts(factory) == (1, 0, 0)
                current = await read(factory, memory.id)
                assert current.revision == (2 if action == "edit" else 1)
                assert (current.archived_at is not None) == (action == "archive")
            finally:
                release.set()
                tasks = [t for t in (writer, mutator) if t is not None]
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(asyncio.wait_for(scenario(), 90))


def test_owned_migration_annotates_only_compatible_revision_one_vectors():
    _require_opt_in()
    async def scenario():
        import json
        config = database_config()
        owned = await create_owned_temporary_database(kind="backup", config=config)
        url = async_database_url(database=owned.name, config=config)
        try:
            await asyncio.to_thread(_upgrade_database, url, "kulai_memory_0003")
            engine = create_async_engine(url)
            ids = [uuid4() for _ in range(5)]
            try:
                async with engine.begin() as connection:
                    for i, memory_id in enumerate(ids):
                        await connection.execute(text("INSERT INTO memories(id,ingestion_id,content,source_kind) VALUES(:id,:ingestion,'synthetic legacy','voice')"), {"id":memory_id,"ingestion":uuid4()})
                        metadata = dict(source_memory_id=str(memory_id), embedding_provider_id="ollama",
                            embedding_model_tag="bge-m3:567m-fp16", embedding_dimension=1024)
                        if i == 1: metadata["embedding_model_tag"] = "incompatible:model"
                        if i == 2: metadata.pop("source_memory_id")
                        if i == 3: metadata["revision"] = 9
                        await connection.execute(text("INSERT INTO kulai_vector_records(namespace_key,record_id,embedding,metadata_json) VALUES(:namespace,:id,CAST(:vector AS vector),CAST(:metadata AS jsonb))"),
                            {"namespace":"other" if i==4 else "kulai_memory.memories.v1", "id":str(memory_id),
                             "vector":"["+",".join(["1"]*1024)+"]", "metadata":json.dumps(metadata)})
                    before = (await connection.execute(text("SELECT record_id,embedding::text FROM kulai_vector_records ORDER BY record_id"))).all()
            finally: await engine.dispose()
            await asyncio.to_thread(_upgrade_database, url, "head")
            assert (await run_database_doctor(async_url=url)).ok
            engine = create_async_engine(url)
            try:
                async with engine.connect() as connection:
                    after = (await connection.execute(text("SELECT record_id,embedding::text FROM kulai_vector_records ORDER BY record_id"))).all()
                    assert before == after
                    data = dict((await connection.execute(text("SELECT record_id,metadata_json FROM kulai_vector_records"))).all())
                    assert data[str(ids[0])]["revision"] == 1
                    assert "revision" not in data[str(ids[1])] and "revision" not in data[str(ids[2])]
                    assert data[str(ids[3])]["revision"] == 9 and "revision" not in data[str(ids[4])]
            finally: await engine.dispose()
        finally: await drop_owned_temporary_database(owned, config=config)
    asyncio.run(asyncio.wait_for(scenario(), 90))


def test_guarded_cli_owned_execute_and_strict_four_backup_restore(tmp_path, monkeypatch):
    _require_opt_in()
    from scripts import manage_memory as cli, db_backup, db_restore_smoke
    async def scenario():
        config = database_config()
        owned = await create_owned_temporary_database(kind="backup", config=config)
        url = async_database_url(database=owned.name, config=config)
        engine = None
        providers = []
        try:
            await asyncio.to_thread(_upgrade_database, url, "head")
            engine = create_async_engine(url)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            memory = await create(factory)
            content = tmp_path / "edit.txt"
            content.write_text("synthetic revised CLI content", encoding="utf-8")
            def fake_indexer(**kwargs):
                provider = CheckedProvider(kwargs["session_factory"].kw["bind"])
                providers.append(provider)
                return indexer(kwargs["session_factory"], provider)
            monkeypatch.setattr(cli, "AutomaticMemoryIndexer", fake_indexer)
            def args(action, sequence, **kwargs):
                values = [action, "--memory-id", str(memory.id), "--execute", "--confirm-main-memory-write",
                          "--backup-output", str(tmp_path / f"cli_{sequence}.dump")]
                if action == "edit": values += ["--expected-revision", "1", "--content-file", str(content)]
                return cli.parser().parse_args(values)
            assert await cli.run(args("edit", 1), owned=owned, source_config=config) == 0
            assert await counts(factory) == (1, 1, 0)
            assert await cli.run(args("archive", 2), owned=owned, source_config=config) == 0
            assert await counts(factory) == (1, 0, 0)
            archived = await snapshot(factory)
            archive = tmp_path / "archived_four.dump"
            await db_backup.create_owned_database_backup(archive, force=False, owned=owned, config=config)
            restored = await db_restore_smoke.restore_owned_database_backup(archive, owned=owned, config=config)
            assert restored.snapshot == archived and restored.snapshot.revision == ("kulai_memory_0004",)
            assert await snapshot(factory) == archived
            assert await cli.run(args("restore", 3), owned=owned, source_config=config) == 0
            assert await counts(factory) == (1, 1, 0)
            current = await read(factory, memory.id)
            assert current.revision == 2 and current.archived_at is None and current.content == content.read_text(encoding="utf-8")
            assert len(providers) == 2 and all(p.calls == 2 and p.closed == 1 for p in providers)
            assert all((tmp_path / f"cli_{n}.dump").is_file() for n in (1,2,3))
        finally:
            if engine is not None: await engine.dispose()
            await drop_owned_temporary_database(owned, config=config)
    asyncio.run(asyncio.wait_for(scenario(), 180))


def test_backfill_and_startup_reconcile_only_active_memories_and_keep_incompatible_revision():
    _require_opt_in()
    async def scenario():
        from kulai_memory.backfill import BackfillReader
        from kulai_memory.application.indexing import MemoryIndexingIncompatibleError
        async with _owned_migrated_session_factory() as factory:
            provider = CheckedProvider(factory.kw["bind"])
            worker = indexer(factory, provider)
            try:
                a,b,c,d = [await create(factory, f"synthetic selection {label}") for label in "ABCD"]
                await worker.ensure(memory=a)
                await mutate_only(factory, "archive", c)
                await worker.ensure(memory=d)
                async with factory() as session:
                    await session.execute(text("UPDATE kulai_vector_records SET metadata_json=jsonb_set(metadata_json,'{revision}','99'::jsonb) WHERE record_id=:id"), {"id":str(d.id)})
                    await session.commit()
                reader = BackfillReader(factory)
                missing = await reader.select(limit=100, memory_id=None, reindex=False)
                assert missing.ids == (b.id,) and missing.incompatible_existing == 1
                assert (await reader.select(limit=100, memory_id=None, reindex=True)).ids == (a.id,b.id,d.id)
                assert (await reader.select(limit=100, memory_id=c.id, reindex=True)).ids == ()
                before = (await snapshot(factory)).memories
                calls = provider.calls
                report = await worker.reconcile()
                assert report.indexed == report.incompatible_existing == 1 and report.remaining_missing == 0
                assert provider.calls == calls+1 and report.degraded
                with pytest.raises(MemoryIndexingIncompatibleError): await worker.ensure(memory=d)
                assert provider.calls == calls+1
                assert (await snapshot(factory)).memories == before and await counts(factory) == (4,3,0)
                async with factory() as session:
                    assert await session.scalar(text("SELECT metadata_json->>'revision' FROM kulai_vector_records WHERE record_id=:id"), {"id":str(d.id)}) == "99"
            finally: await worker.aclose()
    asyncio.run(asyncio.wait_for(scenario(), 90))


def test_cancellation_after_edit_commit_leaves_crash_gap_recoverable():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await create(factory)
            provider = CheckedProvider(factory.kw["bind"])
            worker = indexer(factory, provider)
            try:
                await worker.ensure(memory=memory)
                provider.error = asyncio.CancelledError()
                with pytest.raises(asyncio.CancelledError):
                    await change_memory(action="edit", memory_id=memory.id, content="durable edit before cancellation",
                        expected_revision=1, session_factory=factory, indexer=worker)
                current = await read(factory, memory.id)
                assert current.revision == 2 and current.content == "durable edit before cancellation"
                assert await counts(factory) == (1,0,0)
                before = (await snapshot(factory)).memories
                provider.error = None
                assert (await worker.reconcile()).indexed == 1
                assert await counts(factory) == (1,1,0) and (await snapshot(factory)).memories == before
            finally: await worker.aclose()
    asyncio.run(asyncio.wait_for(scenario(), 90))
