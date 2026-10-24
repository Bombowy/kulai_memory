"""Immediate Desktop deletion on marker-owned PostgreSQL databases only."""
from __future__ import annotations

import asyncio
import traceback
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import Session

from backend.tests.integration.test_memory_postgres import _require_opt_in, _owned_migrated_session_factory
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot, read
from backend.tests.integration.test_desktop_voice_question_postgres import controller, QUERY
from backend.tests.integration.test_desktop_library_postgres import OwnedBGE
from backend.tests.integration.test_memory_rag_postgres import CheckedLLM, DRAGON
from kulai_memory import rag_runtime, deletion_persistence
from kulai_memory.application import Memory, MemoryIngestionRetiredError, MemoryService
from kulai_memory.desktop import controller as controller_module
from kulai_memory.desktop.models import DesktopRagStatus, DesktopDeleteConflictError, DesktopDeleteError
from kulai_memory.persistence import PostgresMemoryRepository


async def prepare(factory, tmp_path, monkeypatch):
    engine = factory.kw['bind']
    bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
    monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
    c, stt, whisper = controller(factory, tmp_path, embedding_provider_factory=lambda **kw: bge)
    await c.startup()
    await c.start_recording(device_id=3)
    note = await c.stop_and_process()
    assert note.memory_id is not None
    return c, note, bge, llm, stt


@pytest.mark.parametrize('archived', [False, True])
def test_desktop_immediate_delete_tombstone_reconnect_and_rag(tmp_path, monkeypatch, archived):
    _require_opt_in()

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            c, note, bge, llm, stt = await prepare(factory, tmp_path, monkeypatch)
            try:
                if archived:
                    await c.archive_memory(memory_id=note.memory_id)
                original = await read(factory, note.memory_id)
                before = await snapshot(factory)
                assert (before.memories.count, before.vectors.count, before.tombstones.count) == (1, int(not archived), 0)
                requests = (bge.requests, len(llm.calls), len(stt.requests))
                result = await c.delete_memory(memory_id=note.memory_id, expected_revision=original.revision)
                assert result.memory_deleted and result.vector_deleted_count == int(not archived)
                assert result.memory_id == original.id
                after = await snapshot(factory)
                assert (after.memories.count, after.vectors.count, after.tombstones.count) == (0, 0, 1)
                assert (bge.requests, len(llm.calls), len(stt.requests)) == requests
                assert await c.list_memory_library() == () and await c.list_recent() == ()
                assert not list(tmp_path.rglob('*.dump'))
                answer = await c.ask_memory(query=QUERY)
                assert answer.status is DesktopRagStatus.INSUFFICIENT_CONTEXT and answer.citations == ()
                assert len(llm.calls) == 0
                for content in (original.content, 'different controlled replay content'):
                    async with factory() as session:
                        with pytest.raises(MemoryIngestionRetiredError):
                            async with session.begin():
                                await MemoryService(repository=PostgresMemoryRepository(db=session)).create_memory_idempotent(
                                    ingestion_id=original.ingestion_id, content=content)
                assert await snapshot(factory) == after
                engine = create_async_engine(factory.kw['bind'].url)
                try:
                    assert await snapshot(async_sessionmaker(engine, expire_on_commit=False)) == after
                finally:
                    await engine.dispose()
            finally:
                await c.shutdown()
            assert bge.closed == llm.closed == 1
    asyncio.run(asyncio.wait_for(scenario(), 60))


@pytest.mark.parametrize('missing', [False, True])
def test_stale_revision_or_missing_id_cannot_delete_current_memory(tmp_path, monkeypatch, missing):
    _require_opt_in()

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            c, note, _, _, _ = await prepare(factory, tmp_path, monkeypatch)
            try:
                real_delete = controller_module.delete_canonical_memory
                state = []

                async def racing_delete(**kwargs):
                    from kulai_memory.lifecycle_persistence import change_memory
                    await change_memory(action='edit', memory_id=note.memory_id, expected_revision=1,
                        content='synthetic concurrent revision two', session_factory=factory, indexer=c._indexer)
                    state.append(await snapshot(factory))
                    return await real_delete(**kwargs)

                if not missing:
                    monkeypatch.setattr(controller_module, 'delete_canonical_memory', racing_delete)
                before = await snapshot(factory)
                with pytest.raises(DesktopDeleteConflictError):
                    await c.delete_memory(memory_id=UUID(int=777) if missing else note.memory_id, expected_revision=1)
                assert (await read(factory, note.memory_id)).revision == (1 if missing else 2)
                assert await snapshot(factory) == (before if missing else state[0])
                assert (await snapshot(factory)).tombstones.count == 0
                assert not list(tmp_path.rglob('*.dump'))
            finally:
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 60))


@pytest.mark.parametrize('stage', ['memory', 'vector', 'commit', 'cancel'])
def test_desktop_transaction_failure_rolls_back_memory_vector_and_tombstone(tmp_path, monkeypatch, stage):
    _require_opt_in()

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            c, note, _, _, _ = await prepare(factory, tmp_path, monkeypatch)
            before = await snapshot(factory)
            original_repository = deletion_persistence.PostgresMemoryRepository
            original_store = deletion_persistence.PgVectorStore

            class Repository(original_repository):
                async def delete_by_id(self, memory_id, *, expected_revision=None):
                    result = await super().delete_by_id(memory_id, expected_revision=expected_revision)
                    if stage == 'memory':
                        raise RuntimeError('PRIVATE_DELETE_TRANSACTION')
                    return result

            class Store(original_store):
                async def delete(self, request):
                    result = await super().delete(request)
                    if stage == 'vector':
                        raise RuntimeError('PRIVATE_DELETE_TRANSACTION')
                    if stage == 'cancel':
                        raise asyncio.CancelledError()
                    return result

            monkeypatch.setattr(deletion_persistence, 'PostgresMemoryRepository', Repository)
            monkeypatch.setattr(deletion_persistence, 'PgVectorStore', Store)

            class RejectCommit(Session):
                pass

            def reject(session):
                raise RuntimeError('PRIVATE_DELETE_TRANSACTION')

            if stage == 'commit':
                event.listen(RejectCommit, 'before_commit', reject)
                c._session_factory = async_sessionmaker(factory.kw['bind'], sync_session_class=RejectCommit)
            try:
                with pytest.raises(asyncio.CancelledError if stage == 'cancel' else DesktopDeleteError) as caught:
                    await c.delete_memory(memory_id=note.memory_id, expected_revision=1)
                assert 'PRIVATE_DELETE_TRANSACTION' not in ''.join(traceback.format_exception(caught.value))
                assert await snapshot(factory) == before
                assert c._delete_task is None and not c._operation_lock.locked()
                assert not list(tmp_path.rglob('*.dump'))
            finally:
                if stage == 'commit':
                    event.remove(RejectCommit, 'before_commit', reject)
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 60))


def test_shutdown_rolls_back_inflight_delete(tmp_path, monkeypatch):
    _require_opt_in()

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            c, note, _, _, _ = await prepare(factory, tmp_path, monkeypatch)
            before = await snapshot(factory)
            entered, release = asyncio.Event(), asyncio.Event()
            original_store = deletion_persistence.PgVectorStore

            class Store(original_store):
                async def delete(self, request):
                    result = await super().delete(request)
                    entered.set()
                    await release.wait()
                    return result

            monkeypatch.setattr(deletion_persistence, 'PgVectorStore', Store)
            task = asyncio.create_task(c.delete_memory(memory_id=note.memory_id, expected_revision=1))
            try:
                await entered.wait()
                await c.shutdown()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert await snapshot(factory) == before
            finally:
                release.set()
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 60))
