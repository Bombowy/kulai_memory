"""Owned synthetic databases only; the configured main is never queried by an LLM."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

import pytest
from kulai_provider_ollama import provider as llm_module
from kulai_provider_ollama_embeddings import provider as bge_module

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_retrieval_postgres import _observe_reads, _seed_memories
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from backend.tests.integration.test_memory_rag_postgres import DRAGON, SyntheticBGE, CheckedLLM
from backend.tests.test_desktop_controller import FakeProvider, FakeRecorder
from kulai_memory import rag_runtime
from kulai_memory.application import Memory
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.database_safety import database_config_for_database
from kulai_memory.desktop.controller import DesktopController
from kulai_memory.desktop.models import DesktopRagProgressState, DesktopRagStatus
from kulai_memory.settings import Settings


def controller(factory, tmp_path, **kwargs):
    engine = factory.kw['bind']
    whisper_counts = dict(creations=0, inference=0)
    def whisper(settings):
        whisper_counts['creations'] += 1
        provider = FakeProvider(('unused',))
        async def forbidden(request):
            whisper_counts['inference'] += 1
            pytest.fail('A text question must never invoke Whisper')
        provider.transcribe = forbidden
        return provider
    c = DesktopController(settings=Settings(_env_file=None, kulai_vector_dimension=1024,
        kulai_llm_model='qwen3.5:9b', kulai_ollama_base_url='http://127.0.0.1:11434'),
        recorder=FakeRecorder(tmp_path), provider_factory=whisper,
        database_config_factory=lambda: database_config_for_database(engine.url.database),
        engine_factory=lambda config: engine, session_factory_builder=lambda ignored: factory, **kwargs)
    return c, whisper_counts


async def readonly_ask(factory, c, **kwargs):
    before = await snapshot(factory)
    statements, commits, remove = _observe_reads(factory.kw['bind'])
    try:
        result = await c.ask_memory(**kwargs)
    finally:
        remove()
    after = await snapshot(factory)
    assert before == after and not commits
    assert 'SELECT' in statements and 'SET' in statements
    return result, before, after


def test_owned_desktop_ask_snapshots_success_insufficient_failure_and_cancellation(tmp_path, monkeypatch):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
            dragon = Memory(content=DRAGON)
            await _seed_memories(factory, (dragon,))
            class OwnedBGE(SyntheticBGE):
                async def aclose(self):
                    self.closed += 1
            bge, llm = OwnedBGE(engine), CheckedLLM(engine, dragon)
            monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kwargs: llm)
            progress = []
            c, whisper = controller(factory, tmp_path, embedding_provider_factory=lambda **kw: bge,
                                    progress_callback=progress.append)
            try:
                startup = await c.startup()  # actual DB doctor and reconciliation/indexing
                assert startup.indexing.indexed == 1 and bge.requests == 1 and llm.calls == []
                assert c._indexer.provider is c._embedding_provider is c._rag._retrieval._provider is bge
                result, before, after = await readonly_ask(factory, c, query='Gdzie mieszka zielony smok?')
                assert result.status is DesktopRagStatus.ANSWERED
                assert result.citations[0].memory_id == dragon.id and result.citations[0].rank == 1
                assert [p.state for p in progress] == [DesktopRagProgressState.RETRIEVING, DesktopRagProgressState.GENERATING]
                print('desktop.owned.snapshot_before=' + json.dumps(asdict(before), sort_keys=True))
                print('desktop.owned.snapshot_after=' + json.dumps(asdict(after), sort_keys=True))

                llm.output = dict(answer='discarded', used_memory_ids=[], sufficient_context=False)
                insufficient, _, _ = await readonly_ask(factory, c, query='Ulubiony instrument smoka?')
                assert insufficient.status is DesktopRagStatus.INSUFFICIENT_CONTEXT
                assert insufficient.answer == INSUFFICIENT_CONTEXT_ANSWER and insufficient.citations == ()
                llm.error = RuntimeError('PRIVATE_CONTEXT_AND_PAYLOAD_SENTINEL')
                failed, _, _ = await readonly_ask(factory, c, query='Generation failure')
                assert failed.status is DesktopRagStatus.FAILED
                assert failed.answer == '' and failed.citations == () and 'PRIVATE' not in repr(failed)

                started = asyncio.Event()
                original_generate = llm.generate
                async def block(request):
                    assert engine.pool.checkedout() == 0
                    started.set()
                    await asyncio.sleep(180)
                    return await original_generate(request)
                llm.generate = block
                cancellation_before = await snapshot(factory)
                statements, commits, remove = _observe_reads(engine)
                try:
                    task = asyncio.create_task(c.ask_memory(query='Cancelled question'))
                    await asyncio.wait_for(started.wait(), timeout=5)
                    await asyncio.wait_for(c.shutdown(), timeout=2)
                    assert task.cancelled()
                finally:
                    remove()
                assert not commits and 'SELECT' in statements and 'SET' in statements
                assert await snapshot(factory) == cancellation_before == before
            finally:
                await c.shutdown()
            assert whisper == dict(creations=1, inference=0)
            assert bge.closed == llm.closed == 1 and bge.requests == 5 and len(llm.calls) == 3
            assert llm.checkout_observations == [0, 0, 0]
            print('desktop.owned=PASS;ask_writes=0;snapshots_equal=success,insufficient,failure,cancellation;'
                  'bge_clients=1;bge_requests=5;bge_closed=1;llm_clients=1;llm_closed=1;checkout_during_llm=0')
    asyncio.run(asyncio.wait_for(scenario(), timeout=120))


def test_real_owned_desktop_shared_bge_qwen_grounding_and_insufficient(tmp_path, monkeypatch):
    _require_opt_in()
    if not all(os.environ.get(name) == '1' for name in ('KULAI_RUN_OLLAMA_INTEGRATION', 'KULAI_RUN_LLM_INTEGRATION')):
        pytest.skip('Enable Ollama and LLM integrations for real synthetic Desktop RAG.')
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
            dragon = Memory(content=DRAGON)
            await _seed_memories(factory, (dragon,))
            bge_counts = dict(clients=0, requests=0, closed=0, native_dimension=1024, dimensions_override_count=0)
            llm_counts = dict(clients=0, requests=0, closed=0, model='qwen3.5:9b')
            checkouts = []
            class BGEClient(bge_module.AsyncClient):
                def __init__(self, *args, **kwargs):
                    bge_counts['clients'] += 1
                    super().__init__(*args, **kwargs)
                async def embed(self, *args, **kwargs):
                    assert engine.pool.checkedout() == 0
                    bge_counts['requests'] += 1
                    bge_counts['dimensions_override_count'] += int(kwargs.get('dimensions') is not None)
                    response = await super().embed(*args, **kwargs)
                    assert len(response.embeddings[0]) == 1024
                    return response
                async def close(self):
                    bge_counts['closed'] += 1
                    await super().close()
            class LLMClient(llm_module.AsyncClient):
                def __init__(self, *args, **kwargs):
                    llm_counts['clients'] += 1
                    super().__init__(*args, **kwargs)
                async def chat(self, *args, **kwargs):
                    checkouts.append(engine.pool.checkedout())
                    assert checkouts[-1] == 0 and kwargs['model'] == 'qwen3.5:9b'
                    llm_counts['requests'] += 1
                    return await super().chat(*args, **kwargs)
                async def close(self):
                    llm_counts['closed'] += 1
                    await super().close()
            monkeypatch.setattr(bge_module, 'AsyncClient', BGEClient)
            monkeypatch.setattr(llm_module, 'AsyncClient', LLMClient)
            c, whisper = controller(factory, tmp_path)
            try:
                startup = await c.startup()
                assert startup.indexing.indexed == 1 and llm_counts['requests'] == 0
                assert c._indexer.provider is c._embedding_provider is c._rag._retrieval._provider
                result, before, after = await readonly_ask(factory, c, query='Gdzie obecnie mieszka zielony smok?')
                assert result.status is DesktopRagStatus.ANSWERED
                assert 'wenus' in result.answer.lower() or 'venus' in result.answer.lower()
                assert [x.memory_id for x in result.citations] == [dragon.id]
                print('desktop.real.pl=PASS;mentions_venus=true;exact_memory_citation=true;status=ANSWERED')
                unsupported, _, _ = await readonly_ask(factory, c, query='Jaki jest ulubiony instrument smoka?')
                assert unsupported.status is DesktopRagStatus.INSUFFICIENT_CONTEXT
                assert unsupported.answer == INSUFFICIENT_CONTEXT_ANSWER and unsupported.citations == ()
                assert await snapshot(factory) == before == after
                print('desktop.real.unsupported=PASS;status=INSUFFICIENT_CONTEXT;citations=0;invented_facts=false')
                print('desktop.real.snapshot_before=' + json.dumps(asdict(before), sort_keys=True))
                print('desktop.real.snapshot_after=' + json.dumps(asdict(after), sort_keys=True))
            finally:
                await c.shutdown()
            assert whisper == dict(creations=1, inference=0)
            assert bge_counts == dict(clients=1, requests=3, closed=1, native_dimension=1024, dimensions_override_count=0)
            assert llm_counts == dict(clients=1, requests=2, closed=1, model='qwen3.5:9b')
            assert checkouts == [0, 0]
            print('desktop.real.whisper=' + json.dumps(whisper, sort_keys=True))
            print('desktop.real.bge=' + json.dumps(bge_counts, sort_keys=True))
            print('desktop.real.llm=' + json.dumps(llm_counts, sort_keys=True))
            print('desktop.real.checkout_during_llm=' + json.dumps(checkouts))
    asyncio.run(asyncio.wait_for(scenario(), timeout=600))
