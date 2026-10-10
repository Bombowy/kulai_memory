from __future__ import annotations
import asyncio
import traceback
from uuid import UUID

import pytest
from backend.tests.test_desktop_controller import Harness
from backend.tests.speech_fakes import FakeSpeechProvider, FakePlayback, PlannerLLM, MIXED, MIXED_PARTS, speech_plan
from kulai_memory.application.speech import plan_speech, SpeechPlan, SpeechSegment, SpeechLanguage
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.desktop.models import (
    DesktopRagResult, DesktopRagCitation, DesktopRagStatus, DesktopSpeechProgress,
    DesktopSpeechProgressState, DesktopSpeechError, DesktopSpeechUnavailableError, DesktopStateError, DesktopVoiceMode,
)
from kulai_memory.speech_runtime import SpeechRuntime


def setup(tmp_path):
    h = Harness(tmp_path)
    llm = PlannerLLM(MIXED_PARTS)
    provider, player = FakeSpeechProvider(), FakePlayback()
    runtime = SpeechRuntime(provider=provider, playback=player)
    c = h.controller()
    c._speech_runtime_factory = lambda **kw: runtime
    async def plan(*, answer):
        return await plan_speech(answer=answer, provider=llm)
    h.rag.plan_speech = plan
    result = DesktopRagResult(DesktopRagStatus.ANSWERED, MIXED, (DesktopRagCitation(UUID(int=91), 2, 0.87),))
    return h, c, llm, provider, player, runtime, result


def test_speak_result_immutable_reused_runtime_no_db_whisper_bge_or_indexing(tmp_path):
    async def scenario():
        h, c, llm, provider, player, runtime, result = setup(tmp_path)
        assert (await c.startup()).tts_available and provider.prepared == 1 and not provider.calls
        sessions, citations = len(h.sessions.sessions), result.citations
        for _ in range(2):
            await c.speak_answer(result=result)
            assert result.answer == MIXED and result.citations is citations
            assert c._speech is runtime and not runtime.artifacts
        assert len(llm.calls) == 2 and len(provider.calls) == 6
        assert len(h.sessions.sessions) == sessions and h.repository.create_or_get_calls == []
        assert h.provider.requests == h.embedding.requests == h.indexer.calls == []
        assert [p.state for p in h.progress if isinstance(p, DesktopSpeechProgress)] == list(DesktopSpeechProgressState) * 2
        assert all(not p.exists() for p in player.paths)
        await c.shutdown()
        await c.shutdown()
        assert provider.closed == h.embedding.closed == h.rag.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


def test_insufficient_context_speaks_canonical_pl_without_qwen(tmp_path):
    async def scenario():
        h, c, llm, provider, _, _, _ = setup(tmp_path)
        await c.startup()
        result = DesktopRagResult(DesktopRagStatus.INSUFFICIENT_CONTEXT, INSUFFICIENT_CONTEXT_ANSWER)
        await c.speak_answer(result=result)
        assert not llm.calls and result.citations == ()
        assert provider.calls == [SpeechSegment(INSUFFICIENT_CONTEXT_ANSWER, SpeechLanguage.PL)]
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['unconfigured', 'factory', 'prepare'])
def test_tts_unavailable_does_not_break_ready_rag_or_voice(tmp_path, failure):
    async def scenario():
        h, c, _, provider, _, _, result = setup(tmp_path)
        if failure == 'unconfigured':
            c._speech_runtime_factory = lambda **kw: None
        elif failure == 'factory':
            def fail(**kw):
                raise RuntimeError('PRIVATE_SETTINGS_PATH')
            c._speech_runtime_factory = fail
        else:
            async def fail():
                raise RuntimeError('PRIVATE_PROVIDER_PATH')
            provider.prepare = fail
        assert not (await c.startup()).tts_available
        with pytest.raises(DesktopSpeechUnavailableError, match='^TTS unavailable\\.$'):
            await c.speak_answer(result=result)
        assert (await c.ask_memory(query='controlled question')).status is DesktopRagStatus.INSUFFICIENT_CONTEXT
        await c.start_recording(device_id=3)
        assert (await c.stop_and_process()).memory_id is not None
        await c.shutdown()
        assert provider.closed == int(failure == 'prepare')
        assert h.provider_factory_calls == h.embedding_factory_calls == h.rag_factory_calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['planner', 'rewritten', 'synthesis', 'playback'])
def test_secondary_failures_safe_and_original_result_preserved(tmp_path, failure):
    async def scenario():
        h, c, llm, provider, player, runtime, result = setup(tmp_path)
        await c.startup()
        if failure == 'planner':
            llm.error = RuntimeError('PRIVATE_ANSWER_PROMPT_PAYLOAD')
        elif failure == 'rewritten':
            llm.output['segments'][0]['text'] = 'translated'
        elif failure == 'synthesis':
            provider.error_at = 2
        else:
            player.error = True
        with pytest.raises(DesktopSpeechError) as caught:
            await c.speak_answer(result=result)
        assert 'PRIVATE' not in ''.join(traceback.format_exception(caught.value))
        assert MIXED not in str(caught.value) and str(caught.value) == 'Could not speak answer.'
        assert result.answer == MIXED and result.citations[0].memory_id == UUID(int=91)
        assert not runtime.artifacts and not c._operation_lock.locked()
        await c.shutdown()
        assert provider.closed == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['recording', 'pending', 'operation'])
def test_speech_serializes_with_existing_operations(tmp_path, mode):
    async def scenario():
        h, c, llm, provider, _, _, result = setup(tmp_path)
        await c.startup()
        if mode == 'recording':
            c._voice_mode = DesktopVoiceMode.NOTE
        elif mode == 'pending':
            c._pending = object()
        else:
            await c._operation_lock.acquire()
        with pytest.raises(DesktopStateError):
            await c.speak_answer(result=result)
        assert not llm.calls and not provider.calls
        c._voice_mode = c._pending = None
        if mode == 'operation':
            c._operation_lock.release()
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['stop', 'ask', 'note', 'voice_question'])
def test_stop_or_new_operation_cleans_playback_then_next_speak_works(tmp_path, operation):
    async def scenario():
        h, c, _, provider, player, runtime, result = setup(tmp_path)
        await c.startup()
        player.release.clear()
        task = asyncio.create_task(c.speak_answer(result=result))
        await player.entered.wait()
        paths = runtime.artifacts
        if operation == 'stop':
            await c.stop_audio()
        elif operation == 'ask':
            await c.ask_memory(query='controlled question')
        elif operation == 'note':
            await c.start_recording(device_id=3)
        else:
            await c.start_voice_question(device_id=3)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not p.exists() for p in paths) and not runtime.artifacts and provider.closed == 0
        if operation in {'note', 'voice_question'}:
            # Finish the separately requested recording through its existing path.
            if operation == 'note':
                await c.stop_and_process()
            else:
                await c.stop_voice_question_and_ask()
        player.release.set()
        await c.speak_answer(result=result)
        await c.shutdown()
        assert provider.closed == 1
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize('phase', ['planner', 'synthesis', 'playback'])
def test_shutdown_cancels_drains_then_closes_every_provider_once(tmp_path, phase):
    async def scenario():
        h, c, _, provider, player, runtime, result = setup(tmp_path)
        await c.startup()
        entered, release = asyncio.Event(), asyncio.Event()
        if phase == 'planner':
            async def plan(**kw):
                entered.set()
                await release.wait()
            h.rag.plan_speech = plan
        elif phase == 'synthesis':
            from kulai_memory.local_tts import drain_audio_work
            original = provider.synthesize
            async def synthesize(segment):
                async def work():
                    entered.set()
                    await release.wait()
                    return await original(segment)
                return await drain_audio_work(asyncio.create_task(work()))
            provider.synthesize = synthesize
        else:
            player.release.clear()
            entered = player.entered
        task = asyncio.create_task(c.speak_answer(result=result))
        await entered.wait()
        paths = runtime.artifacts
        shutdown = asyncio.create_task(c.shutdown())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if phase == 'synthesis':
            assert not shutdown.done() and provider.closed == 0
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await shutdown
        await c.shutdown()
        assert all(not p.exists() for p in paths) and runtime._directory is None
        assert provider.closed == h.embedding.closed == h.rag.closed == h.engine.dispose_count == 1
        assert not any(isinstance(p, DesktopSpeechProgress) and p.state is DesktopSpeechProgressState.FINISHED for p in h.progress)
        assert c._speech_task is None
    asyncio.run(asyncio.wait_for(scenario(), 5))
