"""Opt-in real CUDA Whisper on locally generated synthetic speech, never user audio."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import asdict

import pytest

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from backend.tests.integration.test_memory_retrieval_postgres import _observe_reads
from backend.tests.integration.test_memory_rag_postgres import DRAGON, SyntheticBGE, CheckedLLM
from backend.tests.voice_question_fakes import OwnedWavRecorder
from kulai_memory import rag_runtime
from kulai_memory.application import Memory
from kulai_memory.database_safety import database_config_for_database
from kulai_memory.desktop.controller import DesktopController
from kulai_memory.desktop.models import DesktopRagStatus
from kulai_memory.settings import Settings
from kulai_memory.whisper_provider import create_whisper_transcription_provider


def synthetic_speech(root, text, name):
    """Windows speech synthesis is solely a test fixture generator, not product TTS."""
    path = root / name
    script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $speaker.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,
        [System.Speech.Synthesis.VoiceAge]::NotSet, 0,
        [System.Globalization.CultureInfo]::GetCultureInfo('en-US'))
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $speaker.SetOutputToWaveFile($env:KULAI_TEST_WAV, $format)
    $speaker.Speak($env:KULAI_TEST_TEXT)
} finally { $speaker.Dispose() }
"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            env=dict(os.environ, KULAI_TEST_WAV=str(path), KULAI_TEST_TEXT=text),
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0 and path.exists(), 'Local controlled speech fixture generation failed.'
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def test_real_whisper_note_and_question_share_one_model_and_clean_audio(tmp_path, monkeypatch):
    _require_opt_in()
    if os.environ.get('KULAI_RUN_WHISPER_INTEGRATION') != '1':
        pytest.skip('Enable Whisper integration for controlled real CUDA speech recognition.')
    if os.name != 'nt':
        pytest.skip('This controlled speech fixture generator requires Windows System.Speech.')
    audio = (synthetic_speech(tmp_path, DRAGON, 'synthetic-note.wav'),
             synthetic_speech(tmp_path, 'Where does the green dragon live?', 'synthetic-question.wav'))
    class SpeechRecorder(OwnedWavRecorder):
        def start(self, *, device_id):
            super().start(device_id=device_id)
            self.active_path.write_bytes(audio[self.index - 1])
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw['bind']
            class OwnedBGE(SyntheticBGE):
                async def aclose(self):
                    self.closed += 1
            bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
            monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kw: llm)
            counts = dict(providers_created=0, models_loaded=0, inference_requests=0)
            providers = []
            def whisper(settings):
                counts['providers_created'] += 1
                provider = create_whisper_transcription_provider(settings=settings)
                original_factory, original_transcribe = provider._model_factory, provider.transcribe
                def model_factory(*args, **kwargs):
                    counts['models_loaded'] += 1
                    return original_factory(*args, **kwargs)
                async def transcribe(request):
                    counts['inference_requests'] += 1
                    return await original_transcribe(request)
                provider._model_factory, provider.transcribe = model_factory, transcribe
                providers.append(provider)
                return provider
            c = DesktopController(settings=Settings(_env_file=None, kulai_vector_dimension=1024,
                kulai_whisper_model='large-v3', kulai_whisper_device='cuda',
                kulai_whisper_compute_type='int8_float16', kulai_whisper_vad_filter=True,
                kulai_cuda_dll_dir=Settings().kulai_cuda_dll_dir),
                recorder=SpeechRecorder(tmp_path),
                provider_factory=whisper, embedding_provider_factory=lambda **kw: bge,
                database_config_factory=lambda: database_config_for_database(engine.url.database),
                engine_factory=lambda config: engine, session_factory_builder=lambda ignored: factory)
            try:
                await c.startup()
                await c.start_recording(device_id=3)
                note = await c.stop_and_process()
                assert note.memory_id is not None and 'dragon' in note.transcript.lower()
                model = providers[0]._model
                llm.expected_content = note.transcript
                llm.output['used_memory_ids'] = [str(note.memory_id)]
                before = await snapshot(factory)
                statements, commits, remove = _observe_reads(engine)
                try:
                    await c.start_voice_question(device_id=3)
                    result = await c.stop_voice_question_and_ask()
                finally:
                    remove()
                assert 'dragon' in result.transcript.lower()
                assert result.rag_result.status is DesktopRagStatus.ANSWERED
                assert [x.memory_id for x in result.rag_result.citations] == [note.memory_id]
                assert not commits and 'SELECT' in statements and 'SET' in statements
                assert c.provider is providers[0] and providers[0]._model is model
                assert not list(tmp_path.glob('*.wav'))
                after = await snapshot(factory)
                assert after == before and before.memories.count == before.vectors.count == 1
                print('voice.whisper.real=PASS;model=large-v3;device=cuda;compute=int8_float16;vad=true;'
                      f'question_chars={len(result.transcript)};audio_retained=0;question_writes=0')
                print('voice.whisper.counters=' + json.dumps(counts, sort_keys=True))
                print('voice.whisper.snapshot_before=' + json.dumps(asdict(before), sort_keys=True))
                print('voice.whisper.snapshot_after=' + json.dumps(asdict(after), sort_keys=True))
            finally:
                await c.shutdown()
            assert counts == dict(providers_created=1, models_loaded=1, inference_requests=2)
            assert bge.requests == 2 and bge.closed == llm.closed == 1 and not c._recorder.owned
    asyncio.run(asyncio.wait_for(scenario(), 300))
