"""Permanent deletion/recovery on owned synthetic DBs, never on main."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import Session

from backend.tests.integration.test_memory_postgres import _require_opt_in, _upgrade_database, _owned_migrated_session_factory
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot, read
from backend.tests.integration.test_memory_deletion_postgres import _seed
from backend.tests.integration.test_desktop_voice_question_postgres import controller, QUERY
from backend.tests.integration.test_desktop_library_postgres import OwnedBGE, real_clients
from backend.tests.integration.test_memory_rag_postgres import CheckedLLM, DRAGON
from kulai_memory import database_safety as safety, backup_service, rag_runtime, deletion_persistence
from kulai_memory.application import Memory, MemoryIngestionRetiredError, MemoryService
from kulai_memory.application.deletion import MemoryDeletionRevisionConflictError, MemoryDeletionError
from kulai_memory.desktop import controller as controller_module
from kulai_memory.desktop.models import DesktopRagStatus, DesktopDeleteConflictError, DesktopBackupError
from kulai_memory.persistence import PostgresMemoryRepository


@asynccontextmanager
async def owned_source():
    config = safety.database_config()
    owned = await safety.create_owned_temporary_database(kind='backup', config=config)
    url = safety.async_database_url(database=owned.name, config=config)
    engine = None
    try:
        await asyncio.to_thread(_upgrade_database, url, 'head')
        engine = create_async_engine(url)
        yield async_sessionmaker(engine, expire_on_commit=False), owned, config
    finally:
        if engine is not None:
            await engine.dispose()
        await safety.drop_owned_temporary_database(owned, config=config)


async def recover_retained_dump(archive, expected, config):
    restored = await safety.create_owned_temporary_database(kind='restore', config=config)
    try:
        await safety.restore_archive_to_owned_database(archive, restored, config=config)
        url = safety.async_database_url(database=restored.name, config=config)
        assert (await safety.run_database_doctor(async_url=url)).ok
        assert await safety.database_snapshot_url(url) == expected
    finally:
        await safety.drop_owned_temporary_database(restored, config=config)


async def prepare(factory, owned, config, tmp_path, monkeypatch, *, real=False):
    engine = factory.kw['bind']
    counts = real_clients(engine, monkeypatch) if real else None
    kwargs = {}
    if not real:
        bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
        kwargs['embedding_provider_factory'] = lambda **kw: bge
        monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
    async def backup(output, **kw):
        assert engine.pool.checkedout() == 0
        return await backup_service.create_verified_backup(output, config=config, owned=owned,
            app_env='test', on_verifying=kw['on_verifying'])
    c, stt, whisper = controller(factory, tmp_path, verified_backup_factory=backup, **kwargs)
    await c.startup()
    await c.start_recording(device_id=3)
    note = await c.stop_and_process()
    assert note.memory_id is not None
    return c, note, (counts if real else (bge, llm)), stt, whisper


@pytest.mark.parametrize('archived,real', [(False, False), (True, False), (False, True)],
                         ids=['active', 'archived', 'real_bge'])
def test_desktop_verified_delete_retained_recovery_replay_and_rag(tmp_path, monkeypatch, archived, real):
    _require_opt_in()
    if real and not all(os.environ.get(n) == '1' for n in ('KULAI_RUN_OLLAMA_INTEGRATION', 'KULAI_RUN_LLM_INTEGRATION')):
        pytest.skip('Enable real BGE and LLM provider integrations.')
    async def scenario():
        async with owned_source() as (factory, owned, config):
            c, note, providers, stt, whisper = await prepare(factory, owned, config, tmp_path, monkeypatch, real=real)
            try:
                if archived:
                    await c.archive_memory(memory_id=note.memory_id)
                original = await read(factory, note.memory_id)
                before = await snapshot(factory)
                archive = tmp_path / 'retained_before_delete.dump'
                bge_before = providers[0]['requests'] if real else providers[0].requests
                llm_before = providers[1]['requests'] if real else len(providers[1].calls)
                stt_before = len(stt.requests)
                result = await c.delete_memory(memory_id=note.memory_id, expected_revision=1, backup_output=archive)
                assert result.memory_deleted and result.vector_deleted_count == int(not archived)
                assert result.backup_size > 0 and result.backup_sha256 == backup_service.sha256_file(archive)
                after = await snapshot(factory)
                assert (after.memories.count, after.vectors.count, after.tombstones.count) == (0, 0, 1)
                assert (providers[0]['requests'] if real else providers[0].requests) == bge_before
                assert (providers[1]['requests'] if real else len(providers[1].calls)) == llm_before
                assert len(stt.requests) == stt_before
                assert await c.list_memory_library() == () and await c.list_recent() == ()
                answer = await c.ask_memory(query=QUERY)
                assert answer.status is DesktopRagStatus.INSUFFICIENT_CONTEXT and answer.citations == ()
                assert (providers[1]['requests'] if real else len(providers[1].calls)) == llm_before == 0
                assert await snapshot(factory) == after
                for content in (original.content, 'different controlled replay content'):
                    async with factory() as session:
                        with pytest.raises(MemoryIngestionRetiredError):
                            async with session.begin():
                                await MemoryService(repository=PostgresMemoryRepository(db=session)).create_memory_idempotent(
                                    ingestion_id=original.ingestion_id, content=content)
                assert await snapshot(factory) == after
                # Verify retained pre-delete state in a SECOND owned restore, without restoring source.
                await recover_retained_dump(archive, before, config)
                assert await snapshot(factory) == after and archive.is_file()
                async with factory() as session:
                    async with session.begin():
                        fresh = await PostgresMemoryRepository(db=session).create(Memory(content='fresh identity works'))
                assert fresh.id != original.id and (await snapshot(factory)).vectors.count == 0
                print('desktop.delete.' + ('archived' if archived else 'active') + '.before=' + json.dumps(asdict(before), sort_keys=True))
                print('desktop.delete.after=' + json.dumps(asdict(after), sort_keys=True))
                print('desktop.delete.recovery=PASS;actual_dump_restore=true;retained=true;replay_same_and_different=retired;fresh_identity=PASS;delete_model_requests=0')
                print('desktop.delete.backup=' + json.dumps(dict(path=str(archive), size=result.backup_size, sha256=result.backup_sha256)))
            finally:
                await c.shutdown()
            assert whisper['providers_created'] == 1 and len(stt.requests) == 1
            if real:
                assert providers[0]['clients'] == providers[0]['closed'] == 1
                assert providers[1]['clients'] == providers[1]['closed'] == 1 and providers[1]['requests'] == 0
                print('desktop.delete.providers=' + json.dumps(dict(bge=providers[0], qwen=providers[1], whisper=whisper), sort_keys=True))
            else:
                assert providers[0].closed == providers[1].closed == 1
    asyncio.run(asyncio.wait_for(scenario(), 180))


@pytest.mark.parametrize('phase', ['after_backup', 'after_revalidation'])
def test_stale_selection_after_verified_backup_cannot_delete_new_revision(tmp_path, monkeypatch, phase):
    _require_opt_in()
    async def scenario():
        async with owned_source() as (factory, owned, config):
            c, note, _, _, _ = await prepare(factory, owned, config, tmp_path, monkeypatch)
            try:
                real_backup = c._verified_backup_factory
                real_revalidate = controller_module.revalidate_verified_backup
                state = []
                async def edit():
                    from kulai_memory.lifecycle_persistence import change_memory
                    await change_memory(action='edit', memory_id=note.memory_id, expected_revision=1,
                        content='The green dragon currently lives on Jupiter.', session_factory=factory, indexer=c._indexer)
                    state.append(await snapshot(factory))
                async def racing_backup(*args, **kw):
                    result = await real_backup(*args, **kw)
                    await edit()
                    return result
                async def racing_revalidate(*args, **kw):
                    await real_revalidate(*args, **kw)
                    # Also exercise revision protection AFTER the fresh token check.
                    await edit()
                if phase == 'after_backup':
                    c._verified_backup_factory = racing_backup
                else:
                    monkeypatch.setattr(controller_module, 'revalidate_verified_backup', racing_revalidate)
                archive = tmp_path / 'retained_stale.dump'
                error = DesktopBackupError if phase == 'after_backup' else DesktopDeleteConflictError
                with pytest.raises(error):
                    await c.delete_memory(memory_id=note.memory_id, expected_revision=1, backup_output=archive)
                assert (await read(factory, note.memory_id)).revision == 2
                assert await snapshot(factory) == state[0] and state[0].tombstones.count == 0 and archive.exists()
                print(f'desktop.delete.stale.{phase}=PASS;revision=2;memory_vector_tombstones_unchanged=true;backup_retained=true')
            finally:
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 120))


@pytest.mark.parametrize('change', ['archive', 'restore', 'other_memory', 'file_size', 'file_hash', 'none'])
def test_pre_delete_revalidation_after_verified_factory_return(tmp_path, monkeypatch, change):
    """Inject the race at entry to production revalidation, after the factory returned."""
    _require_opt_in()
    async def scenario():
        async with owned_source() as (factory, owned, config):
            c, note, providers, stt, _ = await prepare(factory, owned, config, tmp_path, monkeypatch)
            try:
                if change == 'restore':
                    await c.archive_memory(memory_id=note.memory_id)
                other = await _seed(factory) if change == 'other_memory' else None
                selected = await read(factory, note.memory_id)
                before = await snapshot(factory)
                real_backup = c._verified_backup_factory
                real_revalidate = controller_module.revalidate_verified_backup
                real_delete = controller_module.delete_canonical_memory
                order, state, tokens = [], [], []
                async def backup(*args, **kw):
                    result = await real_backup(*args, **kw)
                    tokens.append(result)
                    order.append('factory_returned')
                    return result
                async def revalidate(token, **kw):
                    assert order == ['factory_returned'] and token is tokens[0]
                    order.append('pre_delete_revalidation')
                    from kulai_memory.lifecycle_persistence import change_memory
                    if change in {'archive', 'restore', 'other_memory'}:
                        await change_memory(action='archive' if change != 'restore' else 'restore',
                            memory_id=other.id if other else note.memory_id,
                            session_factory=factory, indexer=c._indexer)
                    elif change == 'file_size':
                        with token.path.open('ab') as handle:
                            handle.write(b'controlled post-verification tamper')
                    elif change == 'file_hash':
                        with token.path.open('r+b') as handle:
                            handle.seek(-1, 2)
                            last = handle.read(1)
                            handle.seek(-1, 2)
                            handle.write(bytes([last[0] ^ 1]))
                        assert token.path.stat().st_size == token.size_bytes
                    current = await read(factory, note.memory_id)
                    assert current.revision == selected.revision == 1
                    if change == 'other_memory':
                        assert current == selected
                    if change == 'archive':
                        assert current.archived_at is not None
                    if change == 'restore':
                        assert current.archived_at is None
                    state.append(await snapshot(factory))
                    assert (state[0] != token.source_snapshot) == (change in {'archive', 'restore', 'other_memory'})
                    await real_revalidate(token, **kw)
                    order.append('revalidation_passed')
                async def delete(**kw):
                    order.append('delete')
                    return await real_delete(**kw)
                c._verified_backup_factory = backup
                monkeypatch.setattr(controller_module, 'revalidate_verified_backup', revalidate)
                monkeypatch.setattr(controller_module, 'delete_canonical_memory', delete)
                archive = tmp_path / 'retained_gap.dump'
                requests = (providers[0].requests, len(providers[1].calls), len(stt.requests))
                if change == 'none':
                    result = await c.delete_memory(memory_id=note.memory_id, expected_revision=1, backup_output=archive)
                    assert result.memory_deleted and result.vector_deleted_count == 1
                    assert order == ['factory_returned', 'pre_delete_revalidation', 'revalidation_passed', 'delete']
                    after = await snapshot(factory)
                    assert (after.memories.count, after.vectors.count, after.tombstones.count) == (0, 0, 1)
                    await recover_retained_dump(archive, before, config)
                else:
                    with pytest.raises(DesktopBackupError, match='Memory was not deleted; retain any backup file'):
                        await c.delete_memory(memory_id=note.memory_id, expected_revision=1, backup_output=archive)
                    assert order == ['factory_returned', 'pre_delete_revalidation']
                    assert await snapshot(factory) == state[0] and state[0].tombstones.count == 0
                    assert await read(factory, note.memory_id) is not None
                assert archive.is_file()
                # Only the intentional restore mutation embeds; delete itself never calls models.
                assert providers[0].requests == requests[0] + int(change == 'restore')
                assert (len(providers[1].calls), len(stt.requests)) == requests[1:]
                print(f'desktop.delete.gap.{change}=PASS;factory_returned_before_mutation=true;'
                      f'delete_calls={int(change == "none")};selected_revision=1;backup_retained=true')
            finally:
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 120))


@pytest.mark.parametrize('failure', ['restore', 'source_change'])
def test_real_restore_failure_or_source_change_blocks_delete(tmp_path, monkeypatch, failure):
    _require_opt_in()
    async def scenario():
        async with owned_source() as (factory, owned, config):
            c, note, _, _, _ = await prepare(factory, owned, config, tmp_path, monkeypatch)
            try:
                before = await snapshot(factory)
                expected = [before]
                original_restore = backup_service.restore_archive_to_owned_database
                async def restore(*args, **kw):
                    if failure == 'restore':
                        raise RuntimeError('PRIVATE_RESTORE_PAYLOAD')
                    await original_restore(*args, **kw)
                    from kulai_memory.lifecycle_persistence import change_memory
                    await change_memory(action='edit', memory_id=note.memory_id, expected_revision=1,
                        content='controlled external change', session_factory=factory, indexer=c._indexer)
                    expected[0] = await snapshot(factory)
                monkeypatch.setattr(backup_service, 'restore_archive_to_owned_database', restore)
                archive = tmp_path / 'retained_failed_verification.dump'
                with pytest.raises(DesktopBackupError):
                    await c.delete_memory(memory_id=note.memory_id, expected_revision=1, backup_output=archive)
                assert await snapshot(factory) == expected[0] and expected[0].tombstones.count == 0 and archive.exists()
                print(f'desktop.delete.{failure}=PASS;delete_writes=0;backup_retained=true')
            finally:
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 120))


@pytest.mark.parametrize('stage', ['vector', 'commit', 'cancel'])
def test_revision_safe_delete_transaction_rolls_back_all_tables(monkeypatch, stage):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            memory = await _seed(factory)
            before = await snapshot(factory)
            class Store(deletion_persistence.PgVectorStore):
                async def delete(self, request):
                    result = await super().delete(request)
                    if stage == 'vector':
                        raise RuntimeError('PRIVATE_VECTOR_DELETE')
                    if stage == 'cancel':
                        raise asyncio.CancelledError()
                    return result
            monkeypatch.setattr(deletion_persistence, 'PgVectorStore', Store)
            class RejectCommit(Session):
                pass
            def reject(session):
                raise RuntimeError('PRIVATE_COMMIT')
            deleting = factory
            if stage == 'commit':
                event.listen(RejectCommit, 'before_commit', reject)
                deleting = async_sessionmaker(factory.kw['bind'], sync_session_class=RejectCommit)
            try:
                with pytest.raises(asyncio.CancelledError if stage == 'cancel' else MemoryDeletionError):
                    await deletion_persistence.delete_memory(memory_id=memory.id, expected_revision=1, session_factory=deleting)
            finally:
                if stage == 'commit':
                    event.remove(RejectCommit, 'before_commit', reject)
            assert await snapshot(factory) == before
            print(f'desktop.delete.rollback.{stage}=PASS;complete_snapshot_unchanged=true')
    asyncio.run(asyncio.wait_for(scenario(), 60))


@pytest.mark.parametrize('phase', ['dump', 'verification', 'committed'])
def test_owned_shutdown_during_admin_or_after_commit(tmp_path, monkeypatch, phase):
    _require_opt_in()
    async def scenario():
        async with owned_source() as (factory, owned, config):
            c, note, _, _, _ = await prepare(factory, owned, config, tmp_path, monkeypatch)
            before = await snapshot(factory)
            entered, release = threading.Event(), threading.Event()
            original_tool = backup_service.run_postgres_tool
            original_restore = backup_service.restore_archive_to_owned_database
            original_delete = controller_module.delete_canonical_memory
            def tool(*args, **kw):
                if phase == 'dump':
                    entered.set()
                    assert release.wait(15)
                return original_tool(*args, **kw)
            async def restore(*args, **kw):
                if phase == 'verification':
                    entered.set()
                    await asyncio.to_thread(release.wait)
                return await original_restore(*args, **kw)
            async def committed(**kw):
                result = await original_delete(**kw)
                entered.set()
                await asyncio.to_thread(release.wait)
                return result
            monkeypatch.setattr(backup_service, 'run_postgres_tool', tool)
            monkeypatch.setattr(backup_service, 'restore_archive_to_owned_database', restore)
            if phase == 'committed':
                monkeypatch.setattr(controller_module, 'delete_canonical_memory', committed)
            archive = tmp_path / 'retained_cancelled.dump'
            task = asyncio.create_task(c.delete_memory(memory_id=note.memory_id, expected_revision=1, backup_output=archive))
            try:
                assert await asyncio.to_thread(entered.wait, 20)
                shutdown = asyncio.create_task(c.shutdown())
                await asyncio.sleep(0)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await shutdown
                after = await snapshot(factory)
                if phase == 'committed':
                    assert (after.memories.count, after.vectors.count, after.tombstones.count) == (0, 0, 1)
                else:
                    assert after == before
                assert archive.exists()
                print(f'desktop.delete.cancel.{phase}=PASS;backup_retained=true;durable_delete={phase == "committed"}')
            finally:
                release.set()
                await c.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 120))
