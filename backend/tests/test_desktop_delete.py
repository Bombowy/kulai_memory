"""Delete orchestration, privacy, serialization and cancellation without network."""
from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from dataclasses import fields
from dataclasses import replace
from uuid import UUID

import pytest

from backend.tests.test_desktop_controller import Harness
from kulai_memory.desktop import controller as module
from kulai_memory.desktop.models import (
    DesktopDeleteInputError, DesktopBackupError, DesktopDeleteError, DesktopDeleteConflictError,
    DesktopDeleteProgressState, DesktopStateError, DesktopVoiceMode, DesktopMemoryDeleteResult,
)
from kulai_memory import backup_service
from kulai_memory.backup_service import VerifiedBackupResult, drain_before_cancellation
from kulai_memory.database_safety import DatabaseSnapshot, MemoryFingerprint, VectorFingerprint, TombstoneFingerprint, DoctorReport, CheckResult
from kulai_memory.application.deletion import MemoryDeletionResult, MemoryDeletionRevisionConflictError

PRIVATE = 'PRIVATE_CONTENT_PASSWORD_PROVIDER_PAYLOAD'
ID = UUID(int=71)
SNAPSHOT = DatabaseSnapshot(('kulai_memory_0004',), MemoryFingerprint(1, 'a' * 64),
    VectorFingerprint(1, 'b' * 64), TombstoneFingerprint(0, 'c' * 64))


def setup(tmp_path, monkeypatch, *, failure=None, vector_count=1):
    h = Harness(tmp_path)
    calls = []
    async def backup(output, **kwargs):
        calls.append('backup')
        output.write_bytes(b'PGDMP synthetic retained backup')
        kwargs['on_verifying']()
        if failure == 'backup':
            raise RuntimeError(PRIVATE)
        return VerifiedBackupResult(output, output.stat().st_size, backup_service.sha256_file(output), SNAPSHOT)
    async def snapshot(url):
        return SNAPSHOT
    async def delete(**kwargs):
        calls.append(('delete', kwargs))
        if failure == 'conflict':
            raise MemoryDeletionRevisionConflictError()
        if failure == 'delete':
            raise RuntimeError(PRIVATE)
        return MemoryDeletionResult(kwargs['memory_id'], True, vector_count)
    monkeypatch.setattr(module, 'create_verified_backup', backup)
    monkeypatch.setattr(backup_service, 'database_snapshot_url', snapshot)
    monkeypatch.setattr(module, 'delete_canonical_memory', delete)
    return h, calls


@pytest.mark.parametrize('archived', [False, True])
def test_success_verified_backup_before_revision_safe_delete_no_models(tmp_path, monkeypatch, archived):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch, vector_count=0 if archived else 1)
        c = h.controller()
        await c.startup()
        output = tmp_path / 'retained.dump'
        result = await c.delete_memory(memory_id=ID, expected_revision=4, backup_output=output)
        assert calls[0] == 'backup' and calls[1][0] == 'delete'
        assert calls[1][1] == dict(memory_id=ID, expected_revision=4, session_factory=h.sessions)
        assert result.memory_deleted and result.memory_id == ID and result.vector_deleted_count == int(not archived)
        assert result.backup_path == output and result.backup_size > 0 and result.backup_sha256 == backup_service.sha256_file(output)
        assert {f.name for f in fields(DesktopMemoryDeleteResult)} == {
            'memory_id', 'backup_path', 'backup_size', 'backup_sha256', 'memory_deleted', 'vector_deleted_count'}
        assert [p.state for p in h.progress] == list(DesktopDeleteProgressState)
        assert h.provider.requests == h.embedding.requests == h.indexer.calls == []
        assert h.repository.create_or_get_calls == [] and h.rag.requests == []
        await c.shutdown()
        await c.shutdown()
        assert h.provider_factory_calls == h.embedding.closed == h.rag.closed == h.engine.dispose_count == 1
        assert output.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [('memory_id', 'bad'), ('expected_revision', 0),
    ('expected_revision', True), ('expected_revision', '1'), ('backup_output', None),
    ('backup_output', Path('inside.dump')), ('backup_output', Path('bad.txt'))])
def test_invalid_input_before_doctor_or_providers(tmp_path, monkeypatch, field, value):
    h, calls = setup(tmp_path, monkeypatch)
    args = dict(memory_id=ID, expected_revision=1, backup_output=tmp_path / 'new.dump')
    args[field] = value
    with pytest.raises(DesktopDeleteInputError):
        asyncio.run(h.controller().delete_memory(**args))
    assert calls == [] and not h.sessions.sessions and h.provider_factory_calls == 0


def test_existing_backup_never_overwritten(tmp_path, monkeypatch):
    h, calls = setup(tmp_path, monkeypatch)
    output = tmp_path / 'existing.dump'
    output.write_bytes(b'existing user backup')
    with pytest.raises(DesktopDeleteInputError):
        asyncio.run(h.controller().delete_memory(memory_id=ID, expected_revision=1, backup_output=output))
    assert calls == [] and output.read_bytes() == b'existing user backup'


@pytest.mark.parametrize('failure,error', [('doctor', DesktopBackupError), ('backup', DesktopBackupError),
    ('conflict', DesktopDeleteConflictError), ('delete', DesktopDeleteError)])
def test_failure_safe_and_no_delete_before_verification(tmp_path, monkeypatch, failure, error):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch, failure=failure)
        c = h.controller()
        await c.startup()
        if failure == 'doctor':
            async def bad(url):
                return DoctorReport((CheckResult('schema', False, message=PRIVATE),))
            c._doctor = bad
        output = tmp_path / 'retained.dump'
        with pytest.raises(error) as caught:
            await c.delete_memory(memory_id=ID, expected_revision=1, backup_output=output)
        assert PRIVATE not in ''.join(traceback.format_exception(caught.value))
        assert len(calls) == {'doctor': 0, 'backup': 1, 'conflict': 2, 'delete': 2}[failure]
        assert output.exists() == (failure != 'doctor')
        assert h.provider.requests == h.embedding.requests == h.indexer.calls == []
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['note', 'question', 'pending', 'locked'])
def test_conflicting_operations_rejected(tmp_path, monkeypatch, mode):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        if mode in {'note', 'question'}:
            c._voice_mode = DesktopVoiceMode.NOTE if mode == 'note' else DesktopVoiceMode.QUESTION
        elif mode == 'pending':
            c._pending = object()
        else:
            await c._operation_lock.acquire()
        with pytest.raises(DesktopStateError):
            await c.delete_memory(memory_id=ID, expected_revision=1, backup_output=tmp_path / 'new.dump')
        assert not calls
        c._voice_mode = c._pending = None
        if mode == 'locked':
            c._operation_lock.release()
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['backup', 'verification', 'revalidation', 'delete'])
def test_shutdown_drains_admin_work_and_never_advances_to_delete(tmp_path, monkeypatch, phase):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        async def backup(output, **kwargs):
            async def work():
                output.write_bytes(b'PGDMP retained')
                if phase == 'verification':
                    kwargs['on_verifying']()
                entered.set()
                await release.wait()
                return VerifiedBackupResult(output, 14, 'd' * 64, SNAPSHOT)
            return await drain_before_cancellation(asyncio.create_task(work()))
        async def delete(**kwargs):
            entered.set()
            await release.wait()
            calls.append('unexpected committed delete')
        if phase in {'backup', 'verification'}:
            monkeypatch.setattr(module, 'create_verified_backup', backup)
        elif phase == 'revalidation':
            async def snapshot(url):
                entered.set()
                await release.wait()
                return SNAPSHOT
            monkeypatch.setattr(backup_service, 'database_snapshot_url', snapshot)
        else:
            monkeypatch.setattr(module, 'delete_canonical_memory', delete)
        c = h.controller()
        await c.startup()
        output = tmp_path / 'retained.dump'
        task = asyncio.create_task(c.delete_memory(memory_id=ID, expected_revision=1, backup_output=output))
        await entered.wait()
        with pytest.raises(DesktopStateError):
            await c.ask_memory(query='question')
        shutdown = asyncio.create_task(c.shutdown())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if phase in {'backup', 'verification'}:
            assert not shutdown.done() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await shutdown
        assert calls == ([] if phase in {'backup', 'verification'} else ['backup'])
        assert output.exists() and h.embedding.closed == h.rag.closed == 1 and c._delete_task is None
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize('change', ['archive', 'restore', 'other_memory', 'file_size', 'file_hash', 'none'])
def test_actual_factory_return_to_delete_gap_uses_production_revalidation(tmp_path, monkeypatch, change):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        original_factory = c._verified_backup_factory
        token_returned = []
        async def factory(*args, **kw):
            result = await original_factory(*args, **kw)
            token_returned.append(result)
            return result
        c._verified_backup_factory = factory
        async def snapshot(url):
            # Revalidation is the first read after the verified factory has returned.
            assert len(token_returned) == 1 and calls == ['backup']
            token = token_returned[0]
            if change in {'archive', 'restore'}:
                return replace(SNAPSHOT, vectors=replace(SNAPSHOT.vectors, count=0, sha256='f' * 64))
            if change == 'other_memory':
                return replace(SNAPSHOT, memories=replace(SNAPSHOT.memories, count=2, sha256='f' * 64))
            if change == 'file_size':
                token.path.write_bytes(b'changed size')
            elif change == 'file_hash':
                token.path.write_bytes(b'X' * token.size_bytes)
            return SNAPSHOT
        monkeypatch.setattr(backup_service, 'database_snapshot_url', snapshot)
        output = tmp_path / 'retained.dump'
        try:
            if change == 'none':
                assert (await c.delete_memory(memory_id=ID, expected_revision=1, backup_output=output)).memory_deleted
                assert len(calls) == 2 and calls[1][0] == 'delete'
            else:
                with pytest.raises(DesktopBackupError) as caught:
                    await c.delete_memory(memory_id=ID, expected_revision=1, backup_output=output)
                assert str(caught.value) == 'Backup verification could not be completed. Memory was not deleted; retain any backup file.'
                assert calls == ['backup'] and PRIVATE not in ''.join(traceback.format_exception(caught.value))
            assert output.exists() and h.provider.requests == h.embedding.requests == h.indexer.calls == []
            assert h.rag.requests == [] and h.repository.create_or_get_calls == []
        finally:
            await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('pending', ['shutdown', 'cancellation'])
def test_pending_shutdown_or_cancellation_after_revalidation_never_starts_delete(tmp_path, monkeypatch, pending):
    async def scenario():
        h, calls = setup(tmp_path, monkeypatch)
        c = h.controller()
        await c.startup()
        original = module.revalidate_verified_backup
        async def revalidate(*args, **kw):
            await original(*args, **kw)
            if pending == 'shutdown':
                c._closing = True
            else:
                asyncio.current_task().cancel()
        monkeypatch.setattr(module, 'revalidate_verified_backup', revalidate)
        task = asyncio.create_task(c.delete_memory(memory_id=ID, expected_revision=1,
                                                   backup_output=tmp_path / 'retained.dump'))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == ['backup'] and c._delete_task is None
        # Reset only the injected pending flag so the normal resource cleanup runs.
        c._closing = False
        await c.shutdown()
        assert h.embedding.closed == h.rag.closed == 1
    asyncio.run(scenario())
