"""Retained strict backup and actual restore orchestration fault injection."""
from __future__ import annotations

import asyncio
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from kulai_db import DbConfig
from kulai_memory import backup_service as service
from kulai_memory.database_safety import DoctorReport, CheckResult, OwnedTemporaryDatabase, DatabaseSafetyError
from backend.tests.test_desktop_delete import SNAPSHOT, PRIVATE


def install(monkeypatch, *, failure=None):
    calls = []
    state = SNAPSHOT
    owned = OwnedTemporaryDatabase('kulai_memory_restore_test_' + 'a' * 32, 'marker', 'restore')
    main_thread = threading.get_ident()
    async def doctor(**kw):
        calls.append('doctor')
        return DoctorReport((CheckResult('postgres.version', failure != 'doctor', {'display': '18.6'}),))
    async def snapshot(url, **kw):
        calls.append('snapshot')
        if failure == 'restored' and owned.name in url:
            return replace(state, vectors=replace(state.vectors, sha256='f' * 64))
        if failure == 'source' and 'restore' in calls and owned.name not in url:
            return replace(state, memories=replace(state.memories, sha256='f' * 64))
        return state
    def dump(executable, args, **kw):
        assert threading.get_ident() != main_thread
        calls.append('dump')
        assert PRIVATE not in ' '.join(args)
        Path(args[args.index('--file') + 1]).write_bytes(b'PGDMP controlled archive')
        return subprocess.CompletedProcess(args, 1 if failure == 'dump' else 0, PRIVATE, PRIVATE)
    async def create(**kw):
        calls.append('create')
        return owned
    async def restore(output, *args, **kw):
        calls.append('restore')
        if failure == 'restore':
            raise RuntimeError(PRIVATE)
        if failure == 'hash':
            output.write_bytes(b'PGDMP modified archive')
    async def drop(*args, **kw):
        calls.append('drop')
    monkeypatch.setattr(service, 'run_database_doctor', doctor)
    monkeypatch.setattr(service, 'database_snapshot_url', snapshot)
    monkeypatch.setattr(service, 'find_postgres_tool', lambda n: Path(n))
    monkeypatch.setattr(service, 'postgres_tool_version', lambda p: '18.6')
    monkeypatch.setattr(service, 'run_postgres_tool', dump)
    monkeypatch.setattr(service, 'create_owned_temporary_database', create)
    monkeypatch.setattr(service, 'restore_archive_to_owned_database', restore)
    monkeypatch.setattr(service, 'drop_owned_temporary_database', drop)
    return calls


def config():
    return DbConfig(user='user', password=PRIVATE, name='source',
                    database_url=f'postgresql+asyncpg://user:{PRIVATE}@127.0.0.1/source')


def test_retained_verified_backup_restores_checks_all_fingerprints(tmp_path, monkeypatch):
    calls = install(monkeypatch)
    output = tmp_path / 'new.dump'
    result = asyncio.run(service.create_verified_backup(output, config=config(), app_env='test',
        on_verifying=lambda: calls.append('verifying')))
    assert result.path == output and result.source_snapshot == SNAPSHOT
    assert result.size_bytes == output.stat().st_size and result.sha256 == service.sha256_file(output)
    assert calls.index('dump') < calls.index('verifying') < calls.index('restore') < calls.index('drop')
    assert output.exists()


@pytest.mark.parametrize('failure', ['doctor', 'dump', 'restore', 'restored', 'source', 'hash'])
def test_verification_failure_never_returns_permission_and_retains_dump(tmp_path, monkeypatch, failure):
    calls = install(monkeypatch, failure=failure)
    output = tmp_path / 'retained.dump'
    with pytest.raises((DatabaseSafetyError, RuntimeError)):
        asyncio.run(service.create_verified_backup(output, config=config(), app_env='test'))
    assert output.exists() == (failure != 'doctor')
    if 'create' in calls:
        assert 'drop' in calls


def test_source_change_in_backup_to_verification_gap_rejected(tmp_path, monkeypatch):
    install(monkeypatch)
    async def changed(output, **kw):
        return service.RestoreVerificationResult('owned', replace(SNAPSHOT, memories=replace(SNAPSHOT.memories, sha256='f' * 64)))
    monkeypatch.setattr(service, '_restore_verified_source', changed)
    output = tmp_path / 'retained.dump'
    with pytest.raises(DatabaseSafetyError, match='between backup'):
        asyncio.run(service.create_verified_backup(output, config=config(), app_env='test'))
    assert output.exists()


def test_file_created_after_validation_never_overwritten(tmp_path, monkeypatch):
    install(monkeypatch)
    output = tmp_path / 'raced.dump'
    async def snapshot(url, **kw):
        output.write_bytes(b'other users backup')
        return SNAPSHOT
    monkeypatch.setattr(service, 'database_snapshot_url', snapshot)
    with pytest.raises(FileExistsError):
        asyncio.run(service.create_verified_backup(output, config=config(), app_env='test'))
    assert output.read_bytes() == b'other users backup'


@pytest.mark.parametrize('change', ['memories', 'vectors', 'tombstones', 'revision', 'missing',
                                  'directory', 'size', 'hash', 'symlink', 'snapshot_error', 'none'])
def test_pre_delete_full_snapshot_and_archive_revalidation(tmp_path, monkeypatch, change):
    output = tmp_path / 'retained.dump'
    output.write_bytes(b'PGDMP previously verified backup')
    token = service.VerifiedBackupResult(output, output.stat().st_size, service.sha256_file(output), SNAPSHOT)
    async def snapshot(url):
        assert url == config().async_url
        if change in {'memories', 'vectors', 'tombstones'}:
            fingerprint = getattr(SNAPSHOT, change)
            return replace(SNAPSHOT, **{change: replace(fingerprint, sha256='f' * 64)})
        if change == 'revision':
            return replace(SNAPSHOT, revision=('different_revision',))
        if change == 'snapshot_error':
            raise RuntimeError(PRIVATE)
        return SNAPSHOT
    monkeypatch.setattr(service, 'database_snapshot_url', snapshot)
    if change in {'missing', 'directory'}:
        output.unlink()
        if change == 'directory':
            output.mkdir()
    elif change == 'size':
        output.write_bytes(b'tampered file of another size')
    elif change == 'hash':
        output.write_bytes(b'X' * token.size_bytes)
    elif change == 'symlink':
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda path: path == output or original(path))
    if change == 'none':
        assert asyncio.run(service.revalidate_verified_backup(token, config=config())) is None
    else:
        with pytest.raises(DatabaseSafetyError) as caught:
            asyncio.run(service.revalidate_verified_backup(token, config=config()))
        assert PRIVATE not in str(caught.value) and str(output) not in str(caught.value)
    assert output.exists() == (change != 'missing')


def test_revalidation_cancellation_drains_file_read_and_propagates(tmp_path, monkeypatch):
    async def scenario():
        output = tmp_path / 'retained.dump'
        output.write_bytes(b'PGDMP retained backup')
        token = service.VerifiedBackupResult(output, output.stat().st_size, service.sha256_file(output), SNAPSHOT)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = service.sha256_file
        main_thread = threading.get_ident()
        async def snapshot(url):
            return SNAPSHOT
        def slow_hash(path):
            assert threading.get_ident() != main_thread
            entered.set()
            assert release.wait(5)
            result = original(path)
            finished.set()
            return result
        monkeypatch.setattr(service, 'database_snapshot_url', snapshot)
        monkeypatch.setattr(service, 'sha256_file', slow_hash)
        task = asyncio.create_task(service.revalidate_verified_backup(token, config=config()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set() and output.is_file()
        finally:
            release.set()
    asyncio.run(asyncio.wait_for(scenario(), 10))
