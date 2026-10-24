"""Immediate Desktop deletion without backups, with revision and cancellation checks."""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import fields
from uuid import UUID

import pytest

from backend.tests.test_desktop_controller import Harness
from kulai_memory.desktop import controller as module
from kulai_memory.desktop.models import (
    DesktopDeleteInputError, DesktopDeleteError, DesktopDeleteConflictError,
    DesktopDeleteProgress, DesktopDeleteProgressState, DesktopStateError,
    DesktopVoiceMode, DesktopMemoryDeleteResult,
)
from kulai_memory.application.deletion import MemoryDeletionResult, MemoryDeletionRevisionConflictError

PRIVATE = 'PRIVATE_CONTENT_PASSWORD_PROVIDER_PAYLOAD'
ID = UUID(int=71)


def setup(tmp_path, monkeypatch, *, failure=None, vector_count=1):
    h = Harness(tmp_path)
    calls = []

    async def delete(**kwargs):
        calls.append(kwargs)
        if failure == 'conflict':
            raise MemoryDeletionRevisionConflictError()
        if failure == 'delete':
            raise RuntimeError(PRIVATE)
        return MemoryDeletionResult(kwargs['memory_id'], True, vector_count)

    monkeypatch.setattr(module, 'delete_canonical_memory', delete)
    return h, calls


@pytest.mark.parametrize('archived', [False, True])
def test_immediate_revision_safe_delete_without_backup_or_model_calls(tmp_path, monkeypatch, archived):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch, vector_count=int(not archived))
        c = h.controller()
        await c.startup()

        async def unexpected_doctor(url):
            pytest.fail('DELETE must not run a backup preflight or restore')

        c._doctor = unexpected_doctor
        result = await c.delete_memory(memory_id=ID, expected_revision=4)
        assert calls == [dict(memory_id=ID, expected_revision=4, session_factory=h.sessions)]
        assert result == DesktopMemoryDeleteResult(ID, True, int(not archived))
        assert {f.name for f in fields(DesktopMemoryDeleteResult)} == {
            'memory_id', 'memory_deleted', 'vector_deleted_count'}
        assert h.progress == [DesktopDeleteProgress(DesktopDeleteProgressState.DELETING)]
        assert h.provider.requests == h.embedding.requests == h.indexer.calls == []
        assert h.repository.create_or_get_calls == [] and h.rag.requests == []
        assert not list(tmp_path.rglob('*.dump'))
        await c.shutdown()
        await c.shutdown()
        assert h.provider_factory_calls == h.embedding.closed == h.rag.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [
    ('memory_id', 'bad'), ('memory_id', None),
    ('expected_revision', 0), ('expected_revision', -1),
    ('expected_revision', True), ('expected_revision', '1'), ('expected_revision', None),
])
def test_invalid_id_or_revision_before_database_or_providers(tmp_path, monkeypatch, field, value):
    h, calls = setup(tmp_path, monkeypatch)
    args = dict(memory_id=ID, expected_revision=1)
    args[field] = value
    with pytest.raises(DesktopDeleteInputError):
        asyncio.run(h.controller().delete_memory(**args))
    assert calls == [] and not h.sessions.sessions and h.provider_factory_calls == 0


@pytest.mark.parametrize('failure,error', [
    ('conflict', DesktopDeleteConflictError), ('delete', DesktopDeleteError),
])
def test_failure_is_safe_and_never_returns_success(tmp_path, monkeypatch, failure, error):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch, failure=failure)
        c = h.controller()
        await c.startup()
        with pytest.raises(error) as caught:
            await c.delete_memory(memory_id=ID, expected_revision=1)
        assert PRIVATE not in ''.join(traceback.format_exception(caught.value))
        assert 'backup' not in str(caught.value).lower()
        assert calls == [dict(memory_id=ID, expected_revision=1, session_factory=h.sessions)]
        assert c._delete_task is None and not c._operation_lock.locked()
        assert h.provider.requests == h.embedding.requests == h.indexer.calls == []
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['note', 'question', 'pending', 'locked', 'not_started'])
def test_conflicting_operations_rejected(tmp_path, monkeypatch, mode):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        c = h.controller()
        if mode != 'not_started':
            await c.startup()
        if mode in {'note', 'question'}:
            c._voice_mode = DesktopVoiceMode.NOTE if mode == 'note' else DesktopVoiceMode.QUESTION
        elif mode == 'pending':
            c._pending = object()
        elif mode == 'locked':
            await c._operation_lock.acquire()
        with pytest.raises(DesktopStateError):
            await c.delete_memory(memory_id=ID, expected_revision=1)
        assert not calls
        c._voice_mode = c._pending = None
        if mode == 'locked':
            c._operation_lock.release()
        await c.shutdown()
    asyncio.run(scenario())


def test_shutdown_cancels_inflight_delete_and_releases_resources(tmp_path, monkeypatch):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def delete(**kwargs):
            entered.set()
            await release.wait()
            calls.append('unexpected completed delete')

        monkeypatch.setattr(module, 'delete_canonical_memory', delete)
        c = h.controller()
        await c.startup()
        task = asyncio.create_task(c.delete_memory(memory_id=ID, expected_revision=1))
        await entered.wait()
        with pytest.raises(DesktopStateError):
            await c.ask_memory(query='question')
        await c.shutdown()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not calls and c._delete_task is None and not c._operation_lock.locked()
        assert h.embedding.closed == h.rag.closed == h.engine.dispose_count == 1
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize('pending', ['shutdown', 'cancellation'])
def test_pending_shutdown_or_cancellation_never_starts_transaction(tmp_path, monkeypatch, pending):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()

        def progress(event):
            if pending == 'shutdown':
                c._closing = True
            else:
                asyncio.current_task().cancel()

        c._progress_callback = progress
        task = asyncio.create_task(c.delete_memory(memory_id=ID, expected_revision=1))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [] and c._delete_task is None
        c._closing = False
        await c.shutdown()
    asyncio.run(scenario())
