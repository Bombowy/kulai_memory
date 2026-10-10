"""Real installed Windows PL/EN synthesis; synthetic text, fake audio device only."""
from __future__ import annotations
import asyncio
import json
import os

import pytest
from backend.tests.speech_fakes import PL, EN, FakePlayback, speech_plan
from kulai_memory.application.speech import SpeechLanguage
from kulai_memory.audio_playback import read_wave
from kulai_memory.local_tts import WindowsSpeechProvider
from kulai_memory.speech_runtime import SpeechRuntime


@pytest.mark.parametrize('answer,parts', [
    (PL, [(PL, 'pl')]), (EN, [(EN, 'en')]),
    ('Zielony smok mieszka na Venus. The dragon is happy. Potem zasypia.',
     [('Zielony smok mieszka na ', 'pl'), ('Venus. The dragon is happy. ', 'en'), ('Potem zasypia.', 'pl')]),
], ids=['pl', 'en', 'mixed'])
def test_real_offline_tts_explicit_voices_valid_wav_reuse_cleanup(answer, parts):
    if os.environ.get('KULAI_RUN_TTS_INTEGRATION') != '1':
        pytest.skip('Set KULAI_RUN_TTS_INTEGRATION=1 for installed local PL/EN voices.')
    if os.name != 'nt':
        pytest.skip('Local Windows System.Speech provider requires Windows.')
    async def scenario():
        provider = WindowsSpeechProvider(pl_voice='Microsoft Paulina Desktop', en_voice='Microsoft Zira Desktop')
        player = FakePlayback()
        runtime = SpeechRuntime(provider=provider, playback=player)
        try:
            await runtime.prepare()
            assert provider.clients == 1 and sum(provider.requests.values()) == 0
            await runtime.speak(speech_plan(answer, parts))
            waves = [read_wave(audio) for audio in player.contents]
            assert len(waves) == len(parts) and all(wave.duration > 0 and wave.pcm for wave in waves)
            assert all(audio[:4] == b'RIFF' and audio[8:12] == b'WAVE' for audio in player.contents)
            assert provider.requests == {language: sum(lang == language.value for _, lang in parts) for language in SpeechLanguage}
            assert all(not p.exists() and not p.parent.exists() for p in player.paths) and not runtime.artifacts
            assert provider.clients == 1
            print('tts.real=' + json.dumps({'languages': [lang for _, lang in parts], 'clients': provider.clients,
                'pl_requests': provider.requests[SpeechLanguage.PL], 'en_requests': provider.requests[SpeechLanguage.EN],
                'duration_seconds': [round(wave.duration, 4) for wave in waves], 'format': 'PCM16/WAV',
                'cleanup': True, 'network_inference': False, 'audio_device': 'fake'}))
        finally:
            await runtime.aclose()
            await runtime.aclose()
        assert provider.closed == 1
        print('tts.real.closed=1')
    asyncio.run(asyncio.wait_for(scenario(), 35))
