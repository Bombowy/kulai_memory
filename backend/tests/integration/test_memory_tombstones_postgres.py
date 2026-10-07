from __future__ import annotations

import asyncio
import traceback
from uuid import uuid4

import pytest
from kulai_vector_store import VectorUpsertRequest
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.tests.integration.test_memory_deletion_postgres import (
    _record, _seed, _state, _tombstone_state,
)
from backend.tests.integration.test_memory_postgres import (
    _owned_migrated_session_factory, _require_opt_in, _upgrade_database,
)
from kulai_memory import deletion_persistence
from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE, Memory, MemoryDeletionError, MemoryIngestionRetiredError,
    MemoryService,
)
from kulai_memory.backfill import BackfillReader
from kulai_memory.database_safety import (
    async_database_url, create_owned_temporary_database, database_config,
    drop_owned_temporary_database, run_database_doctor,
)
from kulai_memory.persistence import PostgresMemoryRepository


PRIVATE = "PRIVATE_RETIREMENT_CONTENT_VECTOR_SQL_PASSWORD_SENTINEL"


async def _create_or_retry(session, memory, *, content=None):
    return await MemoryService(repository=PostgresMemoryRepository(db=session)).create_memory_idempotent(
        ingestion_id=memory.ingestion_id,
        content=memory.content if content is None else content,
        session_id=memory.session_id,
        metadata={"diagnostic": "synthetic retry metadata"},
    )


async def _migration_preserves_existing_records():
    config = database_config()
    owned = await create_owned_temporary_database(kind="backup", config=config)
    url = async_database_url(database=owned.name, config=config)
    try:
        await asyncio.to_thread(_upgrade_database, url, "kulai_memory_0002")
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine)
            memory = Memory(content="synthetic migration retention")
            async with factory() as session:
                async with session.begin():
                    # This owned 0002 schema predates repository's tombstone contract.
                    await session.execute(text(
                        "INSERT INTO memories (id, ingestion_id, content, source_kind, metadata_json) "
                        "VALUES (:id, :ingestion_id, :content, 'voice', '{}'::jsonb)"
                    ), {"id": memory.id, "ingestion_id": memory.ingestion_id, "content": memory.content})
                    await PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)).upsert(
                        VectorUpsertRequest(namespace=MEMORY_VECTOR_NAMESPACE, records=(_record(memory),)),
                    )
            before = await BackfillReader(factory).fingerprints()
        finally:
            await engine.dispose()
        await asyncio.to_thread(_upgrade_database, url, "head")
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine)
            assert await BackfillReader(factory).fingerprints() == before
            assert await _tombstone_state(factory) == ()
            doctor = await run_database_doctor(async_url=url)
            assert doctor.ok
            checks = {check.name: check for check in doctor.checks}
            assert checks["alembic.current"].value == ["kulai_memory_0003"]
            assert checks["schema.memory_ingestion_tombstones"].value == {
                "columns": ["deleted_at", "ingestion_id", "memory_id"],
            }
            assert checks["constraint.memory_ingestion_tombstones"].ok
            print("tombstone.migration=PASS;head=kulai_memory_0003;existing_fingerprints_unchanged=true;doctor=PASS")
        finally:
            await engine.dispose()
    finally:
        await drop_owned_temporary_database(owned, config=config)


async def _durable_retirement():
    async with _owned_migrated_session_factory() as factory:
        memory = await _seed(factory)
        async with factory() as session:
            async with session.begin():
                duplicate = await _create_or_retry(session, memory)
                assert not duplicate.created and duplicate.memory.id == memory.id
                assert duplicate.memory.metadata == memory.metadata

        await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=factory)
        tombstones = await _tombstone_state(factory)
        assert len(tombstones) == 1
        row = tombstones[0]
        assert row.ingestion_id == memory.ingestion_id and row.memory_id == memory.id
        assert row.deleted_at.utcoffset() is not None
        for content in (memory.content, "different synthetic content"):
            async with factory() as session:
                with pytest.raises(MemoryIngestionRetiredError) as caught:
                    async with session.begin():
                        await _create_or_retry(session, memory, content=content)
                assert PRIVATE not in "".join(traceback.format_exception(caught.value))
            assert await _state(factory) == ((), ())
            assert await _tombstone_state(factory) == tombstones

        # A non-idempotent repository entrypoint must not bypass retirement.
        async with factory() as session:
            with pytest.raises(MemoryIngestionRetiredError):
                async with session.begin():
                    await PostgresMemoryRepository(db=session).create(memory)
        repeated = await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=factory)
        assert not repeated.memory_deleted and repeated.vector_deleted_count == 0
        assert await _tombstone_state(factory) == tombstones

        fresh = Memory(content=memory.content)
        async with factory() as session:
            async with session.begin():
                created = await _create_or_retry(session, fresh)
                assert created.created and created.memory.ingestion_id == fresh.ingestion_id
        memories, vectors = await _state(factory)
        assert memories == (created.memory.id,) and vectors == ()
        assert await _tombstone_state(factory) == tombstones
        print("tombstone.durable_replay.same_and_different=RETIRED;fresh=CREATED;repeat_delete=PASS")


async def _observe_advisory_wait(engine, *, waiter, holder):
    async def poll():
        async with engine.connect() as connection:
            while True:
                waiting = await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE pid = :waiter AND locktype = 'advisory' AND NOT granted) "
                    "AND :holder = ANY(pg_blocking_pids(:waiter))"
                ), {"waiter": waiter, "holder": holder})
                if waiting:
                    return
                await connection.rollback()
                await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout=5)


async def _retry_delete_race(winner, monkeypatch):
    async with _owned_migrated_session_factory() as factory:
        engine = factory.kw["bind"]
        memory = await _seed(factory)
        held, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = {}
        store_calls = []

        class HoldingSession(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement) and "holder" not in observed:
                    result = await super().execute(text("SELECT pg_backend_pid()"))
                    observed["holder"] = result.scalar_one()
                return await super().execute(statement, *args, **kwargs)

        class WaitingSession(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement) and "waiter" not in observed:
                    result = await super().execute(text("SELECT pg_backend_pid()"))
                    observed["waiter"] = result.scalar_one()
                    attempting.set()
                return await super().execute(statement, *args, **kwargs)

        class HoldingDeleteStore(PgVectorStore):
            async def delete(self, request):
                store_calls.append("vector_delete")
                result = await super().delete(request)
                if winner == "delete":
                    held.set()
                    await release.wait()
                return result

        monkeypatch.setattr(deletion_persistence, "PgVectorStore", HoldingDeleteStore)
        holding_factory = async_sessionmaker(engine, class_=HoldingSession)
        # Separate engine/pool also exercises cross-client database locking.
        waiter_engine = create_async_engine(engine.url)
        waiting_factory = async_sessionmaker(waiter_engine, class_=WaitingSession)

        async def retry(target):
            async with target() as session:
                async with session.begin():
                    result = await _create_or_retry(session, memory)
                    assert not result.created and result.memory.id == memory.id
                    if winner == "retry":
                        held.set()
                        await release.wait()
                return result

        async def delete(target):
            return await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=target)

        tasks = []
        try:
            first, second = (retry, delete) if winner == "retry" else (delete, retry)
            tasks.append(asyncio.create_task(first(holding_factory)))
            await asyncio.wait_for(held.wait(), timeout=5)
            tasks.append(asyncio.create_task(second(waiting_factory)))
            await asyncio.wait_for(attempting.wait(), timeout=5)
            assert observed["holder"] != observed["waiter"]
            await _observe_advisory_wait(engine, waiter=observed["waiter"], holder=observed["holder"])
            assert not tasks[1].done()
        finally:
            release.set()
            try:
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
            finally:
                await waiter_engine.dispose()
        if winner == "retry":
            assert not results[0].created and results[0].memory.id == memory.id
            assert results[1].memory_deleted and results[1].vector_deleted_count == 1
        else:
            assert results[0].memory_deleted and results[0].vector_deleted_count == 1
            assert isinstance(results[1], MemoryIngestionRetiredError)
        assert store_calls == ["vector_delete"]
        assert await _state(factory) == ((), ())
        assert len(await _tombstone_state(factory)) == 1
        print(f"tombstone.race.winner={winner};observed_advisory_wait=true;deadlock=false;tombstones=1;memories=0;vectors=0")


async def _rollback_after_insert(stage):
    async with _owned_migrated_session_factory() as factory:
        memory = await _seed(factory)
        reader = BackfillReader(factory)
        before = await reader.fingerprints()
        observed = []

        class InterruptedSession(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                result = await super().execute(statement, *args, **kwargs)
                if str(statement).startswith("INSERT INTO memory_ingestion_tombstones"):
                    observed.append("inserted")
                    if stage == "cancel":
                        raise asyncio.CancelledError()
                    raise RuntimeError(PRIVATE)
                return result

        interrupting = async_sessionmaker(factory.kw["bind"], class_=InterruptedSession)
        with pytest.raises(asyncio.CancelledError if stage == "cancel" else MemoryDeletionError) as caught:
            await deletion_persistence.delete_memory(memory_id=memory.id, session_factory=interrupting)
        assert observed == ["inserted"]
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert await _tombstone_state(factory) == ()
        assert await reader.fingerprints() == before
        # Rollback releases the advisory lock and identity is still usable.
        async with factory() as session:
            async with session.begin():
                duplicate = await asyncio.wait_for(_create_or_retry(session, memory), timeout=5)
                assert not duplicate.created
        print(f"tombstone.rollback_after_insert={stage};tombstones=0;fingerprints_unchanged=true;lock_released=true")


async def _cancel_waiting_retry():
    async with _owned_migrated_session_factory() as factory:
        engine = factory.kw["bind"]
        memory = await _seed(factory)
        before = await BackfillReader(factory).fingerprints()
        attempting = asyncio.Event()
        observed = {}

        class WaitingSession(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement):
                    result = await super().execute(text("SELECT pg_backend_pid()"))
                    observed["waiter"] = result.scalar_one()
                    attempting.set()
                return await super().execute(statement, *args, **kwargs)

        waiting_factory = async_sessionmaker(engine, class_=WaitingSession)

        async def retry():
            async with waiting_factory() as session:
                async with session.begin():
                    return await _create_or_retry(session, memory)

        async with factory() as holder:
            async with holder.begin():
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                assert not (await _create_or_retry(holder, memory)).created
                task = asyncio.create_task(retry())
                try:
                    await asyncio.wait_for(attempting.wait(), timeout=5)
                    await _observe_advisory_wait(engine, waiter=observed["waiter"], holder=holder_pid)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5)
        assert await BackfillReader(factory).fingerprints() == before
        assert await _tombstone_state(factory) == ()
        async with factory() as session:
            async with session.begin():
                assert not (await asyncio.wait_for(_create_or_retry(session, memory), timeout=5)).created
        print("tombstone.cancel_waiting_retry=PASS;fingerprints_unchanged=true;connection_reusable=true")


def test_real_tombstone_migration_preserves_existing_data_and_doctor_passes():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_migration_preserves_existing_records(), timeout=90))


def test_real_retirement_survives_commit_blocks_all_replays_and_allows_fresh_ingestion():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_durable_retirement(), timeout=90))


@pytest.mark.parametrize("winner", ["retry", "delete"])
def test_real_retry_delete_race_uses_shared_transaction_advisory_lock(winner, monkeypatch):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_retry_delete_race(winner, monkeypatch), timeout=90))


@pytest.mark.parametrize("stage", ["failure", "cancel"])
def test_real_tombstone_insert_rolls_back_on_failure_or_cancellation(stage):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_rollback_after_insert(stage), timeout=90))


def test_real_cancellation_during_advisory_wait_rolls_back_cleanly():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_cancel_waiting_retry(), timeout=90))
