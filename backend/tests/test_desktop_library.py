from __future__ import annotations

import asyncio
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from backend.tests.test_desktop_controller import Harness, FakeSession, InMemoryRepository
from backend.tests.test_memory_deletion import Store
from kulai_memory import lifecycle_persistence
from kulai_memory.application import Memory
from kulai_memory.application.indexing import IndexReconciliationReport
from kulai_memory.desktop import controller as controller_module
from kulai_memory.desktop.models import (
    DesktopMemoryFilter, DesktopMemoryChangeStatus, DesktopMemoryConflictError,
    DesktopLibraryInputError, DesktopLibraryReadError, DesktopMemoryChangeError,
    DesktopMemoryEditInputError, DesktopStateError, DesktopMemoryArchivedError,
)

PRIVATE = 'PRIVATE_MEMORY_OLD_NEW_VECTOR_PASSWORD'


class LibraryRepository(InMemoryRepository):
    def __init__(self, memories):
        super().__init__()
        self.by_ingestion = {m.ingestion_id: m for m in memories}
        self.saved = []

    async def get_for_update(self, memory_id):
        return await self.get_by_id(memory_id)

    async def save_lifecycle(self, memory):
        self.by_ingestion[memory.ingestion_id] = memory
        self.saved.append(memory)
        return memory


def harness(root, monkeypatch, memories=None, **kwargs):
    h = Harness(root, **kwargs)
    memory = Memory(id=UUID(int=1), content=PRIVATE)
    h.repository = LibraryRepository((memory,) if memories is None else memories)
    events = []
    monkeypatch.setattr(lifecycle_persistence, 'PostgresMemoryRepository', lambda **kw: h.repository)
    monkeypatch.setattr(lifecycle_persistence, 'PgVectorStore', lambda **kw: Store(events))
    @asynccontextmanager
    async def begin(session):
        yield session
        await session.commit()
    monkeypatch.setattr(FakeSession, 'begin', begin, raising=False)
    return h, memory, events


def test_library_partition_order_limit_and_neutral_content(tmp_path, monkeypatch):
    async def scenario():
        timestamp = datetime(2025, 1, 1, tzinfo=UTC)
        memories = tuple(Memory(id=UUID(int=i), content=f'full content {i}\n' + 'x' * 250,
            created_at=timestamp + timedelta(days=int(i == 3)),
            archived_at=timestamp if i == 4 else None, metadata={'PRIVATE': 'diagnostics'}) for i in (1, 2, 3, 4))
        h, _, _ = harness(tmp_path, monkeypatch, memories)
        c = h.controller()
        await c.startup()
        active = await c.list_memory_library(limit=2)
        assert [x.id.int for x in active] == [3, 2] and all(not x.archived for x in active)
        archived = await c.list_memory_library(filter=DesktopMemoryFilter.ARCHIVED)
        assert [x.id.int for x in archived] == [4] and archived[0].archived
        assert active[0].content == memories[2].content and active[0].revision == 1
        assert not hasattr(active[0], 'metadata') and 'diagnostics' not in repr(active)
        assert h.provider.requests == h.embedding.requests == h.indexer.calls == []
        assert all(s.closed and not s.commits for s in h.sessions.sessions)
        assert [m.id.int for m in await c.list_recent()] == [3, 2, 1]
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('limit', [0, 101, True, None, '1', 1.5])
def test_invalid_limit_before_io(tmp_path, limit):
    async def scenario():
        h = Harness(tmp_path)
        c = h.controller()
        with pytest.raises(DesktopLibraryInputError):
            await c.list_memory_library(limit=limit)
        assert not h.sessions.sessions and h.provider_factory_calls == 0
    asyncio.run(scenario())


def test_edit_noop_archive_restore_use_real_host_helper_and_same_indexer(tmp_path, monkeypatch):
    async def scenario():
        h, memory, events = harness(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        edited = await c.edit_memory(memory_id=memory.id, expected_revision=1, content='Jupiter')
        assert edited.item.id == memory.id and edited.item.revision == 2 and edited.item.content == 'Jupiter'
        assert edited.status is DesktopMemoryChangeStatus.EDITED and not edited.indexing_degraded
        assert h.indexer.calls[0].revision == 2 and h.sessions.sessions[-1].closed
        unchanged = await c.edit_memory(memory_id=memory.id, expected_revision=2, content='Jupiter')
        assert unchanged.status is DesktopMemoryChangeStatus.UNCHANGED and unchanged.item.revision == 2
        assert len(h.indexer.calls) == len(events) == 1
        archived = await c.archive_memory(memory_id=memory.id)
        assert archived.item.archived and archived.item.revision == 2 and len(h.indexer.calls) == 1
        assert await c.list_memory_library() == ()
        with pytest.raises(DesktopMemoryArchivedError):
            await c.edit_memory(memory_id=memory.id, expected_revision=2, content='forbidden')
        restored = await c.restore_memory(memory_id=memory.id)
        assert not restored.item.archived and restored.item.revision == 2 and len(h.indexer.calls) == 2
        assert c._indexer is h.indexer and c.provider is h.provider
        await c.shutdown()
        await c.shutdown()
        assert h.indexer.closed == h.embedding.closed == h.rag.closed == h.engine.dispose_count == 1
        assert h.provider_factory_calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['edit', 'restore'])
def test_index_failure_after_commit_is_durable_and_retry_does_not_save_again(tmp_path, monkeypatch, action):
    async def scenario():
        h, memory, _ = harness(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        if action == 'restore':
            await c.archive_memory(memory_id=memory.id)
        h.indexer.error = RuntimeError(PRIVATE)
        result = (await c.edit_memory(memory_id=memory.id, content='Jupiter', expected_revision=1) if action == 'edit'
                  else await c.restore_memory(memory_id=memory.id))
        assert result.indexing_degraded and not result.item.archived
        canonical = await h.repository.get_by_id(memory.id)
        assert canonical.revision == (2 if action == 'edit' else 1)
        assert canonical.content == ('Jupiter' if action == 'edit' else PRIVATE)
        assert h.sessions.sessions[-1].commits == 1 and h.sessions.sessions[-1].closed
        saves = len(h.repository.saved)
        h.indexer.report = IndexReconciliationReport(selected=1, indexed=1)
        report = await c.reconcile_missing_indexes()
        assert not report.degraded and len(h.repository.saved) == saves
        await c.shutdown()
    asyncio.run(scenario())


def test_revision_conflict_never_overwrites_and_error_is_private(tmp_path, monkeypatch):
    async def scenario():
        h, memory, _ = harness(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        await c.edit_memory(memory_id=memory.id, content='latest', expected_revision=1)
        calls = len(h.indexer.calls)
        with pytest.raises(DesktopMemoryConflictError) as caught:
            await c.edit_memory(memory_id=memory.id, content=PRIVATE, expected_revision=1)
        assert str(caught.value) == 'Memory changed. Reload it before editing.'
        assert PRIVATE not in ''.join(traceback.format_exception(caught.value))
        assert (await c.list_memory_library())[0].content == 'latest'
        assert len(h.indexer.calls) == calls and len(h.repository.saved) == 1
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('content,revision', [('', 1), (' \n', 1), ('x', True), ('x', 0)])
def test_edit_input_validation_before_db(tmp_path, content, revision):
    async def scenario():
        h = Harness(tmp_path)
        c = h.controller()
        with pytest.raises(DesktopMemoryEditInputError):
            await c.edit_memory(memory_id=UUID(int=1), content=content, expected_revision=revision)
        assert h.sessions.sessions == []
    asyncio.run(scenario())


def test_generic_read_and_write_errors_never_expose_memory(tmp_path, monkeypatch):
    async def scenario():
        h, memory, _ = harness(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        async def fail(**kwargs):
            raise RuntimeError(PRIVATE)
        monkeypatch.setattr(controller_module, 'change_memory', fail)
        with pytest.raises(DesktopMemoryChangeError) as caught:
            await c.archive_memory(memory_id=memory.id)
        assert PRIVATE not in ''.join(traceback.format_exception(caught.value))
        h.repository.list_library = fail
        with pytest.raises(DesktopLibraryReadError) as caught:
            await c.list_memory_library()
        assert PRIVATE not in ''.join(traceback.format_exception(caught.value))
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['note', 'question', 'pending', 'ask'])
def test_library_serializes_with_existing_flows(tmp_path, monkeypatch, mode):
    async def scenario():
        h, memory, _ = harness(tmp_path, monkeypatch, commit_failures=(False, True) if mode == 'pending' else ())
        c = h.controller()
        await c.startup()
        task = None
        release = asyncio.Event()
        if mode == 'ask':
            started = asyncio.Event()
            async def block():
                started.set()
                await release.wait()
            h.rag.before = block
            task = asyncio.create_task(c.ask_memory(query='question'))
            await started.wait()
        elif mode == 'question':
            await c.start_voice_question(device_id=3)
        else:
            await c.start_recording(device_id=3)
            if mode == 'pending':
                assert (await c.stop_and_process()).save_pending
        for operation in (c.list_memory_library(), c.edit_memory(memory_id=memory.id, content='x', expected_revision=1),
                          c.archive_memory(memory_id=memory.id), c.restore_memory(memory_id=memory.id),
                          c.reconcile_missing_indexes()):
            with pytest.raises(DesktopStateError):
                await operation
        release.set()
        if task:
            await task
        await c.shutdown()
    asyncio.run(scenario())


def test_library_shutdown_during_post_commit_indexing_cancels_and_closes_once(tmp_path, monkeypatch):
    async def scenario():
        h, memory, _ = harness(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        started = asyncio.Event()
        async def ensure(**kw):
            assert h.sessions.sessions[-1].closed
            started.set()
            await asyncio.sleep(180)
        h.indexer.ensure = ensure
        task = asyncio.create_task(c.edit_memory(memory_id=memory.id, content='Jupiter', expected_revision=1))
        await started.wait()
        for operation in (c.ask_memory(query='q'), c.start_recording(device_id=3), c.start_voice_question(device_id=3)):
            with pytest.raises(DesktopStateError):
                await operation
        await asyncio.wait_for(c.shutdown(), 1)
        assert task.cancelled() and (await h.repository.get_by_id(memory.id)).revision == 2
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())
