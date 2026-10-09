"""Canonical Library lifecycle/RAG proofs exclusively on owned synthetic databases."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from kulai_provider_ollama import provider as llm_module
from kulai_provider_ollama_embeddings import provider as bge_module

from backend.tests.integration.test_desktop_voice_question_postgres import controller, readonly_question, QUERY
from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_retrieval_postgres import _seed_memories, _observe_reads
from backend.tests.integration.test_memory_lifecycle_postgres import read, snapshot
from backend.tests.integration.test_memory_rag_postgres import DRAGON, SyntheticBGE, CheckedLLM
from kulai_memory import rag_runtime
from kulai_memory.application import Memory
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.library_persistence import read_memory_library
from kulai_memory.desktop.models import DesktopMemoryFilter, DesktopMemoryChangeStatus, DesktopMemoryConflictError, DesktopRagStatus

JUPITER = 'The green dragon currently lives on Jupiter.'


class OwnedBGE(SyntheticBGE):
    error = False

    async def embed(self, request):
        if self.error:
            assert self.engine.pool.checkedout() == 0
            self.requests += 1
            raise RuntimeError('PRIVATE_MEMORY_INDEX_PAYLOAD')
        return await super().embed(request)

    async def aclose(self):
        self.closed += 1


def real_clients(engine, monkeypatch):
    bge = dict(clients=0, requests=0, closed=0, native_dimension=1024, dimensions_override_count=0)
    llm = dict(clients=0, requests=0, closed=0, model='qwen3.5:9b')
    checkouts = []
    class BGEClient(bge_module.AsyncClient):
        def __init__(self, *args, **kwargs):
            bge['clients'] += 1
            super().__init__(*args, **kwargs)
        async def embed(self, *args, **kwargs):
            assert engine.pool.checkedout() == 0
            bge['requests'] += 1
            bge['dimensions_override_count'] += int(kwargs.get('dimensions') is not None)
            result = await super().embed(*args, **kwargs)
            assert len(result.embeddings[0]) == 1024
            return result
        async def close(self):
            bge['closed'] += 1
            await super().close()
    class LLMClient(llm_module.AsyncClient):
        def __init__(self, *args, **kwargs):
            llm['clients'] += 1
            super().__init__(*args, **kwargs)
        async def chat(self, *args, **kwargs):
            checkouts.append(engine.pool.checkedout())
            assert checkouts[-1] == 0 and kwargs['model'] == 'qwen3.5:9b'
            llm['requests'] += 1
            return await super().chat(*args, **kwargs)
        async def close(self):
            llm['closed'] += 1
            await super().close()
    monkeypatch.setattr(bge_module, 'AsyncClient', BGEClient)
    monkeypatch.setattr(llm_module, 'AsyncClient', LLMClient)
    return bge, llm, checkouts


async def readonly_text(factory, c):
    before = await snapshot(factory)
    statements, commits, remove = _observe_reads(factory.kw['bind'])
    try:
        result = await c.ask_memory(query=QUERY)
    finally:
        remove()
    assert await snapshot(factory) == before and not commits and 'SELECT' in statements
    return result


async def vector_revision(factory, memory_id):
    async with factory() as session:
        await session.execute(text('SET TRANSACTION READ ONLY'))
        rows = (await session.execute(text('SELECT record_id, metadata_json, vector_dims(embedding) FROM kulai_vector_records'))).all()
        await session.rollback()
        assert len(rows) == 1 and rows[0][0] == str(memory_id)
        assert rows[0][1]['revision'] == 2 and rows[0][2] == 1024


@pytest.mark.parametrize('real', [False, True], ids=['controlled', 'real_bge_qwen'])
def test_owned_desktop_library_edit_archive_restore_and_rag(tmp_path, monkeypatch, real):
    _require_opt_in()
    if real and not all(os.environ.get(n) == '1' for n in ('KULAI_RUN_OLLAMA_INTEGRATION', 'KULAI_RUN_LLM_INTEGRATION')):
        pytest.skip('Enable real BGE+Qwen integrations for owned Library RAG.')
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
            kwargs = {}
            if real:
                bge_counts, llm_counts, checkouts = real_clients(engine, monkeypatch)
            else:
                bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
                kwargs['embedding_provider_factory'] = lambda **kw: bge
                monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
            c, stt, whisper = controller(factory, tmp_path, **kwargs)
            try:
                await c.startup()
                await c.start_recording(device_id=3)
                note = await c.stop_and_process()
                assert note.memory_id is not None
                original = await read(factory, note.memory_id)
                before = await snapshot(factory)
                statements, commits, remove = _observe_reads(engine)
                try:
                    active = await c.list_memory_library()
                    archived = await c.list_memory_library(filter=DesktopMemoryFilter.ARCHIVED)
                finally:
                    remove()
                assert len(active) == 1 and active[0].id == note.memory_id and active[0].revision == 1 and archived == ()
                assert not commits and await snapshot(factory) == before
                assert c._indexer.provider is c._embedding_provider is c._rag._retrieval._provider
                if real:
                    assert bge_counts['requests'] == 1 and llm_counts['requests'] == 0
                else:
                    assert bge.requests == 1 and llm.calls == []
                    llm.expected_content = JUPITER
                    llm.output.update(answer='Jupiter', used_memory_ids=[str(note.memory_id)])
                edited = await c.edit_memory(memory_id=note.memory_id, expected_revision=1, content=JUPITER)
                assert edited.status is DesktopMemoryChangeStatus.EDITED and not edited.indexing_degraded
                assert edited.item.id == note.memory_id and edited.item.revision == 2
                canonical = await read(factory, note.memory_id)
                assert canonical.model_dump(exclude={'content', 'revision'}) == original.model_dump(exclude={'content', 'revision'})
                await vector_revision(factory, note.memory_id)
                after_edit = await snapshot(factory)
                assert after_edit.memories.count == after_edit.vectors.count == 1
                assert after_edit.memories != before.memories and after_edit.vectors != before.vectors
                assert after_edit.tombstones == before.tombstones
                answer = await readonly_text(factory, c)
                assert answer.status is DesktopRagStatus.ANSWERED and ('jupiter' in answer.answer.lower() or 'jowisz' in answer.answer.lower())
                assert [x.memory_id for x in answer.citations] == [note.memory_id]
                requests = bge_counts['requests'] if real else bge.requests
                unchanged = await c.edit_memory(memory_id=note.memory_id, expected_revision=2, content=JUPITER)
                assert unchanged.status is DesktopMemoryChangeStatus.UNCHANGED and unchanged.item.revision == 2
                assert await snapshot(factory) == after_edit
                with pytest.raises(DesktopMemoryConflictError):
                    await c.edit_memory(memory_id=note.memory_id, expected_revision=1, content='stale editor')
                assert await snapshot(factory) == after_edit and (bge_counts['requests'] if real else bge.requests) == requests

                archived_result = await c.archive_memory(memory_id=note.memory_id)
                assert archived_result.item.archived and archived_result.item.revision == 2
                after_archive = await snapshot(factory)
                assert after_archive.memories.count == 1 and after_archive.vectors.count == 0
                assert after_archive.tombstones == before.tombstones
                assert (bge_counts['requests'] if real else bge.requests) == requests
                assert await c.list_memory_library() == ()
                archived_items = await c.list_memory_library(filter=DesktopMemoryFilter.ARCHIVED)
                assert len(archived_items) == 1 and archived_items[0].content == JUPITER
                assert await c.list_recent() == ()
                text = await readonly_text(factory, c)
                stt._texts = (QUERY,)
                voice, _, _ = await readonly_question(factory, c)
                assert text.status is voice.rag_result.status is DesktopRagStatus.INSUFFICIENT_CONTEXT
                assert text.answer == voice.rag_result.answer == INSUFFICIENT_CONTEXT_ANSWER
                assert text.citations == voice.rag_result.citations == ()
                assert (llm_counts['requests'] if real else len(llm.calls)) == 1

                restored = await c.restore_memory(memory_id=note.memory_id)
                assert not restored.item.archived and restored.item.revision == 2 and not restored.indexing_degraded
                after_restore = await snapshot(factory)
                assert after_restore.memories == after_edit.memories and after_restore.vectors.count == 1
                assert after_restore.tombstones == before.tombstones
                await vector_revision(factory, note.memory_id)
                assert (await c.list_memory_library())[0].id == note.memory_id
                assert await c.list_memory_library(filter=DesktopMemoryFilter.ARCHIVED) == ()
                restored_answer, _, _ = await readonly_question(factory, c)
                assert restored_answer.rag_result.status is DesktopRagStatus.ANSWERED
                assert 'jupiter' in restored_answer.rag_result.answer.lower() or 'jowisz' in restored_answer.rag_result.answer.lower()
                assert [x.memory_id for x in restored_answer.rag_result.citations] == [note.memory_id]
                assert await snapshot(factory) == after_restore
                prefix = 'library.real' if real else 'library.owned'
                for label, value in (('before', before), ('edited', after_edit), ('archived', after_archive), ('restored', after_restore)):
                    print(prefix + '.snapshot_' + label + '=' + json.dumps(asdict(value), sort_keys=True))
                print(prefix + '=PASS;revision=1->2->2->2;edit_rag=Jupiter;archive_text_and_voice=insufficient;'
                      'restore_voice_rag=Jupiter;exact_citations=true;no_op_embedding=0;conflict_writes=0;'
                      'archive_embedding=0;tombstones_unchanged=true')
            finally:
                await c.shutdown()
            assert whisper['providers_created'] == 1 and len(stt.requests) == 3 and not c._recorder.owned
            if real:
                assert bge_counts == dict(clients=1, requests=7, closed=1, native_dimension=1024, dimensions_override_count=0)
                assert llm_counts == dict(clients=1, requests=2, closed=1, model='qwen3.5:9b')
                assert checkouts == [0, 0]
                print('library.real.bge=' + json.dumps(bge_counts, sort_keys=True))
                print('library.real.llm=' + json.dumps(llm_counts, sort_keys=True))
                print('library.real.checkout_during_llm=' + json.dumps(checkouts))
                print('library.real.whisper=providers_created:1,inferences:3;engine=1')
            else:
                assert bge.requests == 7 and bge.closed == llm.closed == 1 and len(llm.calls) == 2
                assert llm.checkout_observations == [0, 0]
    asyncio.run(asyncio.wait_for(scenario(), 600 if real else 120))


@pytest.mark.parametrize('action', ['edit', 'restore'])
def test_owned_desktop_indexing_failure_is_durable_and_retry_only_repairs_vector(tmp_path, monkeypatch, action):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            bge = OwnedBGE(factory.kw['bind'])
            llm = CheckedLLM(factory.kw['bind'], Memory(content=DRAGON))
            monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
            c, _, _ = controller(factory, tmp_path, embedding_provider_factory=lambda **kw: bge)
            try:
                await c.startup()
                await c.start_recording(device_id=3)
                note = await c.stop_and_process()
                if action == 'restore':
                    await c.archive_memory(memory_id=note.memory_id)
                before = await snapshot(factory)
                bge.error = True
                result = (await c.edit_memory(memory_id=note.memory_id, expected_revision=1, content=JUPITER) if action == 'edit'
                          else await c.restore_memory(memory_id=note.memory_id))
                assert result.indexing_degraded and not result.item.archived
                durable = await snapshot(factory)
                assert durable.memories.count == 1 and durable.vectors.count == 0 and durable.tombstones == before.tombstones
                current = (await c.list_memory_library())[0]
                assert current.content == (JUPITER if action == 'edit' else DRAGON)
                assert current.revision == (2 if action == 'edit' else 1)
                requests = bge.requests
                no_op = await c.edit_memory(memory_id=current.id, expected_revision=current.revision, content=current.content)
                assert no_op.status is DesktopMemoryChangeStatus.UNCHANGED and bge.requests == requests
                assert await snapshot(factory) == durable  # missing vector does not turn a noop edit into repair
                bge.error = False
                report = await c.reconcile_missing_indexes()
                after = await snapshot(factory)
                assert report.indexed == 1 and not report.degraded and bge.requests == requests + 1
                assert after.memories == durable.memories and after.tombstones == durable.tombstones and after.vectors.count == 1
                print(f'library.degraded.{action}=PASS;canonical_durable=true;retry_canonical_writes=0;vector_repaired=true;no_op_embedding=0')
            finally:
                await c.shutdown()
            assert bge.closed == llm.closed == 1
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_owned_read_adapter_exact_archived_partition_and_deterministic_ties():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            timestamp = datetime(2025, 1, 1, tzinfo=UTC)
            memories = tuple(Memory(id=UUID(int=i), content='controlled library content', created_at=timestamp,
                archived_at=timestamp if i == 4 else None) for i in (1, 3, 2, 4))
            await _seed_memories(factory, memories)
            before = await snapshot(factory)
            statements, commits, remove = _observe_reads(factory.kw['bind'])
            try:
                active = await read_memory_library(archived=False, limit=2, session_factory=factory)
                archived = await read_memory_library(archived=True, limit=100, session_factory=factory)
            finally:
                remove()
            assert [m.id.int for m in active] == [3, 2] and [m.id.int for m in archived] == [4]
            assert not commits and 'SET' in statements and await snapshot(factory) == before
    asyncio.run(asyncio.wait_for(scenario(), 60))
