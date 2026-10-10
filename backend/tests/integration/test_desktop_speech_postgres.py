"""Read-only speech over owned synthetic Memory; real synthesis, fake audio device."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

import pytest
from kulai_llm import LLMResponse

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from backend.tests.integration.test_memory_retrieval_postgres import _observe_reads
from backend.tests.integration.test_memory_rag_postgres import DRAGON, CheckedLLM
from backend.tests.integration.test_desktop_library_postgres import OwnedBGE, real_clients
from backend.tests.integration.test_desktop_voice_question_postgres import controller, QUERY
from backend.tests.speech_fakes import FakeSpeechProvider, FakePlayback, PL
from kulai_memory import rag_runtime
from kulai_memory.application import Memory
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.application.speech import SpeechLanguage, SPEECH_PLANNER_SYSTEM_PROMPT
from kulai_memory.audio_playback import read_wave
from kulai_memory.desktop.models import DesktopRagStatus, DesktopSpeechError
from kulai_memory.local_tts import WindowsSpeechProvider
from kulai_memory.speech_runtime import SpeechRuntime


class DualLLM(CheckedLLM):
    def __init__(self, engine):
        super().__init__(engine, Memory(content=DRAGON))
        self.planner_calls = []
        self.planner_invalid = False
        self.output['answer'] = PL

    async def generate(self, request):
        if 'segments' not in request.response_format.json_schema['properties']:
            return await super().generate(request)
        assert self.engine.pool.checkedout() == 0
        assert len(request.messages) == 2 and request.messages[0].content == SPEECH_PLANNER_SYSTEM_PROMPT
        data = json.loads(request.messages[1].content.split('\n', 1)[1])
        assert data == {'answer': PL}  # Nothing else: no Memory context/citations.
        self.checkout_observations.append(0)
        self.planner_calls.append(request)
        return LLMResponse(text=json.dumps({'segments': [{'text': 'rewritten' if self.planner_invalid else PL,
                                                          'language': 'pl'}]}),
                           provider_id='ollama', model_id='qwen3.5:9b')


@pytest.mark.parametrize('real', [False, True], ids=['controlled', 'real_bge_qwen_tts'])
def test_owned_desktop_text_voice_speech_readonly_shared_clients(tmp_path, monkeypatch, real):
    _require_opt_in()
    if real and not all(os.environ.get(n) == '1' for n in (
            'KULAI_RUN_OLLAMA_INTEGRATION', 'KULAI_RUN_LLM_INTEGRATION', 'KULAI_RUN_TTS_INTEGRATION')):
        pytest.skip('Enable real local BGE, Qwen and TTS for owned Desktop speech.')
    if real and os.name != 'nt':
        pytest.skip('System.Speech requires Windows.')
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
            kwargs, planned_answers, speech_checkouts = {}, [], []
            if real:
                bge_counts, llm_counts, llm_checkouts = real_clients(engine, monkeypatch)
                provider = WindowsSpeechProvider(pl_voice='Microsoft Paulina Desktop', en_voice='Microsoft Zira Desktop')
            else:
                bge, llm = OwnedBGE(engine), DualLLM(engine)
                kwargs['embedding_provider_factory'] = lambda **kw: bge
                monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
                provider = FakeSpeechProvider()
            player = FakePlayback()
            runtime = SpeechRuntime(provider=provider, playback=player)
            c, stt, whisper = controller(factory, tmp_path, speech_runtime_factory=lambda **kw: runtime, **kwargs)
            original_synthesize = provider.synthesize
            async def synthesize(segment):
                speech_checkouts.append(engine.pool.checkedout())
                assert speech_checkouts[-1] == 0
                return await original_synthesize(segment)
            provider.synthesize = synthesize
            try:
                assert (await c.startup()).tts_available
                assert not speech_checkouts
                await c.start_recording(device_id=3)
                note = await c.stop_and_process()
                assert note.memory_id is not None
                if not real:
                    llm.output['used_memory_ids'] = [str(note.memory_id)]
                stt._texts = (QUERY,)
                before = await snapshot(factory)
                assert c._indexer.provider is c._embedding_provider is c._rag._retrieval._provider
                original_plan = c._rag.plan_speech
                async def plan(*, answer):
                    assert engine.pool.checkedout() == 0
                    planned_answers.append(answer)
                    return await original_plan(answer=answer)
                c._rag.plan_speech = plan
                original_generate = c._rag._llm.generate
                requests = dict(rag=0, speech_plan=0)
                async def generate(request):
                    assert engine.pool.checkedout() == 0
                    speech_request = 'segments' in request.response_format.json_schema['properties']
                    requests['speech_plan' if speech_request else 'rag'] += 1
                    if speech_request:
                        data = json.loads(request.messages[1].content.split('\n', 1)[1])
                        assert data == {'answer': planned_answers[-1]}
                        assert len(request.messages) == 2 and request.messages[0].content == SPEECH_PLANNER_SYSTEM_PROMPT
                    return await original_generate(request)
                c._rag._llm.generate = generate
                statements, commits, remove = _observe_reads(engine)
                try:
                    for voice in (False, True):
                        if voice:
                            await c.start_voice_question(device_id=3)
                            answer = (await c.stop_voice_question_and_ask()).rag_result
                        else:
                            answer = await c.ask_memory(query=QUERY)
                        assert answer.status is DesktopRagStatus.ANSWERED
                        assert 'wenus' in answer.answer.lower() or 'venus' in answer.answer.lower()
                        assert [x.memory_id for x in answer.citations] == [note.memory_id]
                        await c.speak_answer(result=answer)
                        assert answer.citations[0].memory_id == note.memory_id
                        assert await snapshot(factory) == before
                    if not real:
                        llm.output = dict(answer='ignored', used_memory_ids=[], sufficient_context=False)
                    insufficient = await c.ask_memory(query='Jaki jest ulubiony instrument smoka?')
                    assert insufficient.status is DesktopRagStatus.INSUFFICIENT_CONTEXT and not insufficient.citations
                    assert insufficient.answer == INSUFFICIENT_CONTEXT_ANSWER
                    planning_count = requests['speech_plan']
                    await c.speak_answer(result=insufficient)
                    assert requests['speech_plan'] == planning_count
                    if not real:
                        llm.planner_invalid = True
                        with pytest.raises(DesktopSpeechError):
                            await c.speak_answer(result=answer)
                        assert await snapshot(factory) == before and answer.citations[0].memory_id == note.memory_id
                        llm.planner_invalid = False
                        provider.error_at = len(provider.calls) + 1
                        with pytest.raises(DesktopSpeechError):
                            await c.speak_answer(result=answer)
                        assert await snapshot(factory) == before
                        provider.error_at = None
                        for shutdown in (False, True):
                            player.entered.clear()
                            player.release.clear()
                            task = asyncio.create_task(c.speak_answer(result=answer))
                            await player.entered.wait()
                            paths = runtime.artifacts
                            if shutdown:
                                await c.shutdown()
                            else:
                                await c.stop_audio()
                            with pytest.raises(asyncio.CancelledError):
                                await task
                            assert all(not p.exists() for p in paths)
                            assert await snapshot(factory) == before
                finally:
                    remove()
                assert not commits and not runtime.artifacts and all(not p.exists() for p in player.paths)
                assert all(read_wave(audio).duration > 0 for audio in player.contents)
                after = await snapshot(factory)
                assert after == before
                print('speech.desktop.' + ('real' if real else 'owned') + '.snapshot_before=' + json.dumps(asdict(before), sort_keys=True))
                print('speech.desktop.' + ('real' if real else 'owned') + '.snapshot_after=' + json.dumps(asdict(after), sort_keys=True))
                print('speech.desktop=PASS;answer_and_citations_preserved=true;ask_speech_writes=0;checkout_qwen_tts=0;audio_retained=0;audio_device=fake')
            finally:
                await c.shutdown()
                await c.shutdown()
            assert whisper['providers_created'] == 1 and len(stt.requests) == 2
            assert provider.closed == 1
            if real:
                assert bge_counts == dict(clients=1, requests=4, closed=1, native_dimension=1024, dimensions_override_count=0)
                assert llm_counts == dict(clients=1, requests=5, closed=1, model='qwen3.5:9b')
                assert llm_checkouts == [0] * 5 and provider.clients == 1
                tts = dict(clients=provider.clients, closed=provider.closed,
                           pl_requests=provider.requests[SpeechLanguage.PL], en_requests=provider.requests[SpeechLanguage.EN])
            else:
                assert bge.closed == llm.closed == 1 and bge.requests == 4
                bge_counts = dict(clients=1, requests=bge.requests, closed=bge.closed)
                llm_counts = dict(clients=1, requests=len(llm.calls) + len(llm.planner_calls), closed=llm.closed)
                tts = dict(clients=1, closed=provider.closed,
                           pl_requests=sum(s.language is SpeechLanguage.PL for s in provider.calls),
                           en_requests=sum(s.language is SpeechLanguage.EN for s in provider.calls))
            print('speech.desktop.whisper=' + json.dumps(dict(**whisper, inference_requests=len(stt.requests)), sort_keys=True))
            print('speech.desktop.bge=' + json.dumps(bge_counts, sort_keys=True))
            print('speech.desktop.qwen=' + json.dumps(dict(**llm_counts, **requests), sort_keys=True))
            print('speech.desktop.tts=' + json.dumps(tts, sort_keys=True))
    asyncio.run(asyncio.wait_for(scenario(), 600))
