from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from kulai_provider_whisper import WhisperTranscriptionProvider, WhisperProviderConfig

from backend.tests.test_desktop_controller import Harness
from backend.tests.test_desktop_recorder import _recorder
from backend.tests.test_memory_rag import FakeLLM, evidence, PRIVATE
from backend.tests.voice_question_fakes import OwnedWavRecorder
from kulai_memory.application.rag import answer_memory
from kulai_memory.desktop import controller as controller_module
from kulai_memory.desktop.models import (
    DesktopRagProgressState, DesktopRagStatus, DesktopStateError, DesktopRagInputError,
    DesktopVoiceMode, DesktopVoiceQuestionProgressState, DesktopVoiceQuestionTranscriptionError,
)

QUERY = ' Gdzie mieszka zielony smok? '


def harness(root, **kwargs):
    h = Harness(root, **kwargs)
    h.recorder = OwnedWavRecorder(root)
    return h


def test_exact_voice_transcript_uses_public_ask_without_ingestion_or_recent(tmp_path, monkeypatch):
    async def scenario():
        h = harness(tmp_path, texts=(QUERY,))
        h.rag.result = await answer_memory(query=QUERY, retrieval=evidence('Venus'), llm_provider=FakeLLM())
        c = h.controller()
        await c.startup()
        def forbidden_uuid():
            pytest.fail('Question must not allocate an ingestion/session UUID')
        monkeypatch.setattr(controller_module, 'uuid4', forbidden_uuid)
        sessions = len(h.sessions.sessions)
        calls = []
        original_ask = c.ask_memory
        async def ask(**kwargs):
            calls.append(kwargs)
            return await original_ask(**kwargs)
        c.ask_memory = ask
        assert await c.start_voice_question(device_id=3) is None
        assert c._active_ingestion_id is None and c._voice_mode is DesktopVoiceMode.QUESTION
        result = await c.stop_voice_question_and_ask(top_k=3)
        assert result.transcript == QUERY and result.rag_result.status is DesktopRagStatus.ANSWERED
        assert result.rag_result.citations[0].memory_id == evidence('Venus').hits[0].memory.id
        assert calls == [dict(query=QUERY, top_k=3)] and h.rag.requests == [(QUERY, 3)]
        assert [p.state for p in h.progress] == [DesktopVoiceQuestionProgressState.TRANSCRIBING,
            DesktopVoiceQuestionProgressState.TRANSCRIPT_READY, DesktopRagProgressState.RETRIEVING,
            DesktopRagProgressState.GENERATING]
        assert h.progress[1].transcript == QUERY
        assert len(h.sessions.sessions) == sessions and not c.has_pending_save
        assert h.repository.create_or_get_calls == h.indexer.calls == []
        assert len(h.provider.requests) == len(h.recorder.cleaned) == 1
        assert not list(tmp_path.glob('*.wav'))
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['', ' \n\t'])
def test_empty_speech_skips_rag_bge_qwen_and_db(tmp_path, text):
    async def scenario():
        h = harness(tmp_path, texts=(text,))
        c = h.controller()
        await c.startup()
        sessions = len(h.sessions.sessions)
        await c.start_voice_question(device_id=3)
        result = await c.stop_voice_question_and_ask()
        assert result.transcript == text and result.rag_result is None
        assert h.rag.requests == h.embedding.requests == h.indexer.calls == h.repository.create_or_get_calls == []
        assert len(h.sessions.sessions) == sessions and not list(tmp_path.glob('*.wav'))
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('rag_error', [False, True])
def test_insufficient_and_rag_failure_keep_transcript_but_no_writes(tmp_path, rag_error):
    async def scenario():
        h = harness(tmp_path, texts=(QUERY,))
        if rag_error:
            h.rag.error = RuntimeError(PRIVATE)
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=3)
        result = await c.stop_voice_question_and_ask()
        assert result.transcript == QUERY
        expected = DesktopRagStatus.FAILED if rag_error else DesktopRagStatus.INSUFFICIENT_CONTEXT
        assert result.rag_result.status is expected and result.rag_result.citations == ()
        assert PRIVATE not in repr(result)
        assert h.repository.create_or_get_calls == h.indexer.calls == []
        assert not list(tmp_path.glob('*.wav'))
        await c.shutdown()
    asyncio.run(scenario())


def test_whisper_failure_is_safe_and_cleans_audio(tmp_path):
    async def scenario():
        h = harness(tmp_path)
        async def fail(request):
            assert request.audio.path.exists()
            raise RuntimeError(PRIVATE)
        h.provider.transcribe = fail
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=3)
        with pytest.raises(DesktopVoiceQuestionTranscriptionError) as caught:
            await c.stop_voice_question_and_ask()
        assert str(caught.value) == 'Question transcription failed.' and caught.value.__suppress_context__
        assert h.rag.requests == h.repository.create_or_get_calls == h.indexer.calls == []
        assert not list(tmp_path.glob('*.wav'))
        assert c._voice_mode is c._voice_question_task is None
        await c.shutdown()
    asyncio.run(scenario())


def test_explicit_modes_prevent_conflicting_note_question_and_text_ask(tmp_path):
    async def scenario():
        h = harness(tmp_path, texts=('voice note', QUERY))
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=3)
        for operation in (c.start_recording(device_id=3), c.ask_memory(query='text'),
                          c.start_voice_question(device_id=3), c.stop_and_process()):
            with pytest.raises(DesktopStateError):
                await operation
        await c.stop_voice_question_and_ask()
        await c.start_recording(device_id=3)
        for operation in (c.start_voice_question(device_id=3), c.stop_voice_question_and_ask()):
            with pytest.raises(DesktopStateError):
                await operation
        assert c._voice_mode is DesktopVoiceMode.NOTE
        note = await c.stop_and_process()
        assert note.memory_id is not None and len(h.indexer.calls) == 1
        await c.shutdown()
    asyncio.run(scenario())


def test_pending_save_and_active_text_ask_block_voice_question(tmp_path):
    async def scenario():
        h = harness(tmp_path, commit_failures=(False, True))
        c = h.controller()
        await c.startup()
        await c.start_recording(device_id=3)
        assert (await c.stop_and_process()).save_pending
        with pytest.raises(DesktopStateError):
            await c.start_voice_question(device_id=3)
        await c.retry_save()
        started, release = asyncio.Event(), asyncio.Event()
        async def block():
            started.set()
            await release.wait()
        h.rag.before = block
        task = asyncio.create_task(c.ask_memory(query='text'))
        await started.wait()
        with pytest.raises(DesktopStateError):
            await c.start_voice_question(device_id=3)
        release.set()
        await task
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('shutdown', [False, True])
def test_cancel_during_thread_stt_keeps_wav_until_reader_finishes(tmp_path, shutdown):
    async def scenario():
        h = harness(tmp_path, texts=(QUERY,))
        started, release, read_finished = threading.Event(), threading.Event(), threading.Event()
        original = h.provider.transcribe
        def read(path):
            started.set()
            assert release.wait(5)
            assert path.exists() and path.read_bytes().startswith(b'RIFF')
            read_finished.set()
        async def transcribe(request):
            await asyncio.to_thread(read, request.audio.path)
            return await original(request)
        h.provider.transcribe = transcribe
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=3)
        task = asyncio.create_task(c.stop_voice_question_and_ask())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            closer = asyncio.create_task(c.shutdown()) if shutdown else None
            if not shutdown:
                task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done() and list(tmp_path.glob('*.wav'))
            assert h.embedding.closed == h.rag.closed == 0
            task.cancel()  # a repeated cancellation must also drain the reader
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if closer:
            await closer
        else:
            await c.shutdown()
        assert read_finished.is_set() and not list(tmp_path.glob('*.wav'))
        assert h.rag.requests == h.indexer.calls == h.repository.create_or_get_calls == []
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == h.engine.dispose_count == 1
        assert [p.state for p in h.progress] == [DesktopVoiceQuestionProgressState.TRANSCRIBING]
    asyncio.run(scenario())


def test_shutdown_during_question_qwen_is_bounded_and_audio_already_clean(tmp_path):
    async def scenario():
        h = harness(tmp_path, texts=(QUERY,))
        started = asyncio.Event()
        async def block():
            started.set()
            await asyncio.sleep(180)
        h.rag.before = block
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=3)
        task = asyncio.create_task(c.stop_voice_question_and_ask())
        await started.wait()
        assert not list(tmp_path.glob('*.wav'))
        await asyncio.wait_for(c.shutdown(), timeout=1)
        assert task.cancelled() and h.embedding.closed == h.rag.closed == 1
        assert h.repository.create_or_get_calls == h.indexer.calls == []
    asyncio.run(scenario())


def test_close_during_capture_stops_real_bounded_recorder_and_cleans_owned_wav(tmp_path):
    async def scenario():
        h = harness(tmp_path)
        h.recorder, backend = _recorder(tmp_path)
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=2)
        assert list(tmp_path.glob('*.wav'))
        await c.shutdown()
        assert not list(tmp_path.glob('*.wav')) and all(s.closed for s in backend.streams)
        assert h.provider.requests == h.rag.requests == []
    asyncio.run(scenario())


def test_cancel_during_capture_start_drains_start_then_cleans_without_stt(tmp_path):
    async def scenario():
        h = harness(tmp_path)
        started, release = threading.Event(), threading.Event()
        original_start = h.recorder.start
        def start(*, device_id):
            original_start(device_id=device_id)
            started.set()
            assert release.wait(5)
        h.recorder.start = start
        c = h.controller()
        await c.startup()
        task = asyncio.create_task(c.start_voice_question(device_id=3))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done() and list(tmp_path.glob('*.wav'))
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c._voice_mode is None and not h.recorder.owned
        assert h.provider.requests == h.rag.requests == h.repository.create_or_get_calls == []
        await c.shutdown()
    asyncio.run(scenario())


def test_note_and_question_reuse_one_actual_whisper_provider_and_model(tmp_path):
    async def scenario():
        h = harness(tmp_path)
        loads, inference = [], []
        class Model:
            supported_languages = ['en', 'pl']
            def transcribe(self, audio, **kwargs):
                inference.append(audio)
                text = 'synthetic note' if len(inference) == 1 else QUERY
                return (SimpleNamespace(id=0, start=0.0, end=1.0, text=text),), SimpleNamespace(
                    language='en', language_probability=0.99, duration=1.0, duration_after_vad=1.0)
        model = Model()
        def factory(*args, **kwargs):
            loads.append(kwargs)
            return model
        h.provider = WhisperTranscriptionProvider(WhisperProviderConfig(model_size_or_path='large-v3',
            device='cuda', compute_type='int8_float16', vad_filter=True), _model_factory=factory)
        c = h.controller()
        await c.startup()
        await c.start_recording(device_id=3)
        note = await c.stop_and_process()
        await c.start_voice_question(device_id=3)
        result = await c.stop_voice_question_and_ask()
        assert note.memory_id is not None and result.transcript == QUERY.strip()
        assert h.rag.requests == [(QUERY.strip(), 5)]
        assert c.provider is h.provider and h.provider._model is model
        assert h.provider_factory_calls == len(loads) == 1 and len(inference) == 2
        assert len(h.repository.create_or_get_calls) == len(h.indexer.calls) == 1
        assert not list(tmp_path.glob('*.wav'))
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('top_k', [0, 21, True])
def test_question_top_k_validation_does_not_stop_capture(tmp_path, top_k):
    async def scenario():
        h = harness(tmp_path)
        c = h.controller()
        await c.startup()
        await c.start_voice_question(device_id=3)
        with pytest.raises(DesktopRagInputError):
            await c.stop_voice_question_and_ask(top_k=top_k)
        assert h.provider.requests == [] and h.recorder.active_path.exists()
        await c.shutdown()
    asyncio.run(scenario())
