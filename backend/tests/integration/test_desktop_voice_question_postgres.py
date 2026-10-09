"""Voice questions over owned synthetic DBs/audio; never read main data into models."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

import pytest
from kulai_provider_ollama import provider as llm_module
from kulai_provider_ollama_embeddings import provider as bge_module

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_retrieval_postgres import _observe_reads
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from backend.tests.integration.test_memory_rag_postgres import DRAGON, SyntheticBGE, CheckedLLM
from backend.tests.test_desktop_controller import FakeProvider
from backend.tests.voice_question_fakes import OwnedWavRecorder
from kulai_memory import rag_runtime
from kulai_memory.application import Memory
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.database_safety import database_config_for_database
from kulai_memory.desktop.controller import DesktopController
from kulai_memory.desktop.models import DesktopRagStatus, DesktopVoiceQuestionTranscriptionError
from kulai_memory.settings import Settings

QUERY = 'Gdzie mieszka zielony smok?'


def controller(factory, tmp_path, **kwargs):
    engine = factory.kw['bind']
    provider = FakeProvider((DRAGON,))
    counts = dict(providers_created=0)
    def whisper(settings):
        counts['providers_created'] += 1
        return provider
    c = DesktopController(settings=Settings(_env_file=None, kulai_vector_dimension=1024,
        kulai_llm_model='qwen3.5:9b', kulai_ollama_base_url='http://127.0.0.1:11434'),
        recorder=OwnedWavRecorder(tmp_path), provider_factory=whisper,
        database_config_factory=lambda: database_config_for_database(engine.url.database),
        engine_factory=lambda config: engine, session_factory_builder=lambda ignored: factory, **kwargs)
    return c, provider, counts


async def readonly_question(factory, c):
    before = await snapshot(factory)
    statements, commits, remove = _observe_reads(factory.kw['bind'])
    try:
        await c.start_voice_question(device_id=3)
        result = await c.stop_voice_question_and_ask()
    finally:
        remove()
        assert await snapshot(factory) == before and not commits
        assert not c._recorder.owned
    return result, before, statements


def test_owned_voice_question_snapshots_all_outcomes_and_shared_resources(tmp_path, monkeypatch):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
            class OwnedBGE(SyntheticBGE):
                async def aclose(self):
                    self.closed += 1
            bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
            monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
            c, stt, whisper = controller(factory, tmp_path, embedding_provider_factory=lambda **kw: bge)
            try:
                await c.startup()
                assert bge.requests == 0 and llm.calls == []
                await c.start_recording(device_id=3)
                note = await c.stop_and_process()  # fixture uses normal note/save/index path
                assert note.memory_id is not None and bge.requests == 1
                llm.output['used_memory_ids'] = [str(note.memory_id)]
                stt._texts = (QUERY,)
                assert c.provider is stt
                assert c._indexer.provider is c._embedding_provider is c._rag._retrieval._provider is bge
                before = await snapshot(factory)
                assert before.memories.count == before.vectors.count == 1 and before.tombstones.count == 0
                text = await c.ask_memory(query=QUERY)
                assert text.status is DesktopRagStatus.ANSWERED and await snapshot(factory) == before
                sufficient, baseline, statements = await readonly_question(factory, c)
                assert sufficient.transcript == QUERY and sufficient.rag_result.status is DesktopRagStatus.ANSWERED
                assert [x.memory_id for x in sufficient.rag_result.citations] == [note.memory_id]
                assert 'SELECT' in statements and 'SET' in statements and baseline == before

                llm.output = dict(answer='discarded', used_memory_ids=[], sufficient_context=False)
                insufficient, _, _ = await readonly_question(factory, c)
                assert insufficient.rag_result.status is DesktopRagStatus.INSUFFICIENT_CONTEXT
                assert insufficient.rag_result.answer == INSUFFICIENT_CONTEXT_ANSWER
                assert insufficient.rag_result.citations == ()
                stt._texts = ('',)
                empty, _, statements = await readonly_question(factory, c)
                assert empty.rag_result is None and not statements and bge.requests == 4
                original_stt = stt.transcribe
                async def fail(request):
                    stt.requests.append(request)
                    raise RuntimeError('PRIVATE_AUDIO_QUERY_PAYLOAD')
                stt.transcribe = fail
                with pytest.raises(DesktopVoiceQuestionTranscriptionError, match='^Question transcription failed\\.$'):
                    await readonly_question(factory, c)
                assert bge.requests == 4
                stt.transcribe = original_stt
                stt._texts = (QUERY,)
                llm.error = RuntimeError('PRIVATE_CONTEXT_PAYLOAD')
                failed, _, _ = await readonly_question(factory, c)
                assert failed.rag_result.status is DesktopRagStatus.FAILED
                assert failed.rag_result.citations == () and 'PRIVATE' not in repr(failed)

                started = asyncio.Event()
                async def generate(request):
                    assert engine.pool.checkedout() == 0
                    llm.checkout_observations.append(0)
                    started.set()
                    await asyncio.sleep(180)
                llm.generate = generate
                statements, commits, remove = _observe_reads(engine)
                try:
                    await c.start_voice_question(device_id=3)
                    task = asyncio.create_task(c.stop_voice_question_and_ask())
                    await asyncio.wait_for(started.wait(), 5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    remove()
                assert not commits and not c._recorder.owned and await snapshot(factory) == before

                # Cancellation while STT still reads must drain it before removing WAV.
                started, release = asyncio.Event(), asyncio.Event()
                async def transcribe(request):
                    started.set()
                    await release.wait()
                    assert request.audio.path.exists()
                    return await original_stt(request)
                stt.transcribe = transcribe
                statements, commits, remove = _observe_reads(engine)
                try:
                    await c.start_voice_question(device_id=3)
                    task = asyncio.create_task(c.stop_voice_question_and_ask())
                    await started.wait()
                    task.cancel()
                    await asyncio.sleep(0)
                    assert c._recorder.owned
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                    remove()
                assert not commits and not statements and not c._recorder.owned
                after = await snapshot(factory)
                assert after == before
                print('voice.owned.snapshot_before=' + json.dumps(asdict(before), sort_keys=True))
                print('voice.owned.snapshot_after=' + json.dumps(asdict(after), sort_keys=True))
            finally:
                await c.shutdown()
            assert whisper['providers_created'] == 1 and len(stt.requests) == 8
            assert bge.requests == 6 and bge.closed == llm.closed == 1
            assert llm.checkout_observations == [0] * 5 and len(llm.calls) == 4
            print('voice.owned=PASS;snapshots_equal=sufficient,insufficient,empty,stt_failure,rag_failure,cancel_stt,cancel_llm;'
                  'question_writes=0;whisper_providers=1;whisper_inferences=8;bge_clients=1;bge_requests=6;bge_closed=1;'
                  'llm_clients=1;llm_requests=5;llm_closed=1;checkout_during_llm=0;audio_retained=0')
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_real_owned_voice_question_shared_bge_qwen(tmp_path, monkeypatch):
    _require_opt_in()
    if not all(os.environ.get(name) == '1' for name in ('KULAI_RUN_OLLAMA_INTEGRATION', 'KULAI_RUN_LLM_INTEGRATION')):
        pytest.skip('Enable Ollama and LLM integrations for real owned voice-question RAG.')
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
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
            c, stt, whisper = controller(factory, tmp_path)
            try:
                await c.startup()
                assert bge_counts['requests'] == llm_counts['requests'] == 0
                await c.start_recording(device_id=3)
                note = await c.stop_and_process()
                assert note.memory_id is not None and bge_counts['requests'] == 1
                stt._texts = (QUERY,)
                assert c._indexer.provider is c._embedding_provider is c._rag._retrieval._provider
                before = await snapshot(factory)
                text = await c.ask_memory(query=QUERY)
                assert text.status is DesktopRagStatus.ANSWERED and await snapshot(factory) == before
                voice, baseline, _ = await readonly_question(factory, c)
                assert voice.transcript == QUERY and voice.rag_result.status is DesktopRagStatus.ANSWERED
                assert 'wenus' in voice.rag_result.answer.lower() or 'venus' in voice.rag_result.answer.lower()
                assert [x.memory_id for x in voice.rag_result.citations] == [note.memory_id]
                assert baseline == before
                stt._texts = ('Jaki jest ulubiony instrument smoka?',)
                unsupported, _, _ = await readonly_question(factory, c)
                assert unsupported.rag_result.status is DesktopRagStatus.INSUFFICIENT_CONTEXT
                assert unsupported.rag_result.answer == INSUFFICIENT_CONTEXT_ANSWER
                assert unsupported.rag_result.citations == ()
                after = await snapshot(factory)
                assert after == before
                print('voice.real.pl=PASS;mentions_venus=true;exact_citation=true;status=ANSWERED')
                print('voice.real.unsupported=PASS;status=INSUFFICIENT_CONTEXT;citations=0')
                print('voice.real.snapshot_before=' + json.dumps(asdict(before), sort_keys=True))
                print('voice.real.snapshot_after=' + json.dumps(asdict(after), sort_keys=True))
            finally:
                await c.shutdown()
            assert whisper['providers_created'] == 1 and len(stt.requests) == 3
            assert bge_counts == dict(clients=1, requests=4, closed=1, native_dimension=1024, dimensions_override_count=0)
            assert llm_counts == dict(clients=1, requests=3, closed=1, model='qwen3.5:9b')
            assert checkouts == [0, 0, 0]
            print('voice.real.whisper=' + json.dumps(dict(**whisper, inference_requests=len(stt.requests)), sort_keys=True))
            print('voice.real.bge=' + json.dumps(bge_counts, sort_keys=True))
            print('voice.real.llm=' + json.dumps(llm_counts, sort_keys=True))
            print('voice.real.checkout_during_llm=' + json.dumps(checkouts))
    asyncio.run(asyncio.wait_for(scenario(), 600))
