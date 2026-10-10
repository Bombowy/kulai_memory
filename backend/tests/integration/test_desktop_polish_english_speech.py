"""Real Desktop planner, installed voices and production playback; synthetic only."""
from __future__ import annotations
import asyncio
import json
import os
from uuid import UUID

import pytest
from kulai_provider_ollama import provider as ollama_module

from backend.tests.polish_english_fakes import CompleteAudioDevice, PL_EN_CASES, assert_language_coverage
from backend.tests.test_desktop_controller import Harness
from kulai_memory.application.speech import SpeechLanguage, SPEECH_PLANNER_SYSTEM_PROMPT
from kulai_memory.audio_playback import SoundDevicePlayback
from kulai_memory.desktop.models import DesktopRagCitation, DesktopRagResult, DesktopRagStatus
from kulai_memory.local_tts import WindowsSpeechProvider
from kulai_memory.rag_runtime import MemoryRagRuntime
from kulai_memory.speech_runtime import SpeechRuntime


def test_real_controller_qwen_voice_switching_and_production_playback(tmp_path, monkeypatch):
    if not all(os.environ.get(name) == '1' for name in ('KULAI_RUN_LLM_INTEGRATION', 'KULAI_RUN_TTS_INTEGRATION')):
        pytest.skip('Enable real local Qwen and installed Windows PL/EN voices.')
    if os.name != 'nt':
        pytest.skip('System.Speech requires Windows.')
    # All four directions, plus monolingual PL/EN, on ONE provider/process.
    cases = [PL_EN_CASES[i] for i in (0, 1, 2, 3, 10, 11, 6, 8, 12, 13, 14)]
    counts = dict(clients=0, requests=0, closed=0)
    class Client(ollama_module.AsyncClient):
        def __init__(self, *args, **kwargs):
            counts['clients'] += 1
            super().__init__(*args, **kwargs)
        async def chat(self, *args, **kwargs):
            messages = kwargs['messages']
            assert len(messages) == 2 and messages[0]['content'] == SPEECH_PLANNER_SYSTEM_PROMPT
            data = json.loads(messages[1]['content'].split('\n', 1)[1])
            assert set(data) == {'answer'} and data['answer'] in tuple(c[0] for c in cases)
            counts['requests'] += 1
            return await super().chat(*args, **kwargs)
        async def close(self):
            counts['closed'] += 1
            await super().close()
    monkeypatch.setattr(ollama_module, 'AsyncClient', Client)

    async def scenario():
        h = Harness(tmp_path)
        c = h.controller()
        provider = WindowsSpeechProvider(pl_voice='Microsoft Paulina Desktop', en_voice='Microsoft Zira Desktop')
        device = CompleteAudioDevice()
        runtime = SpeechRuntime(provider=provider, playback=SoundDevicePlayback(audio_module=device))
        c._speech_runtime_factory = lambda **kwargs: runtime
        c._rag_runtime_factory = MemoryRagRuntime
        infos, plans = [], []
        original_speak = runtime.speak
        async def speak(plan, **kwargs):
            plans.append(plan)
            await original_speak(plan, on_segment=infos.append, **kwargs)
        runtime.speak = speak
        try:
            assert (await c.startup()).tts_available
            sessions = len(h.sessions.sessions)
            assert provider.clients == 1 and not counts['requests']
            for case_number, (answer, languages, phrases) in enumerate(cases, 1):
                start = len(infos)
                result = DesktopRagResult(DesktopRagStatus.ANSWERED, answer,
                    (DesktopRagCitation(UUID(int=99), 1, .9),))
                await c.speak_answer(result=result)
                assert_language_coverage(plans[-1], answer, languages, phrases)
                current = infos[start:]
                assert [info.language.value for info in current] == list(languages)
                voices = ['Microsoft Paulina Desktop' if lang == 'pl' else 'Microsoft Zira Desktop' for lang in languages]
                assert [info.voice_id for info in current] == voices
                assert [info.number for info in current] == list(range(1, len(current) + 1))
                assert sum(info.character_count for info in current) == len(answer)
                assert result.answer == answer and result.citations[0].memory_id == UUID(int=99)
                assert len(h.sessions.sessions) == sessions and not runtime.artifacts
                print('speech.desktop.routing.case=' + str(case_number) + ';' + json.dumps({
                    'segment_count': len(current), 'languages': list(languages),
                    'char_counts': [info.character_count for info in current], 'voices': voices}))
            assert not h.provider.requests and not h.embedding.requests and not h.indexer.calls
            assert not h.repository.create_or_get_calls
            assert device.maximum_active == 1 and device.active == 0
            assert len(device.streams) == len(infos)
            assert all(stream.aborted == stream.closed == 1 and stream.output for stream in device.streams)
            assert provider.clients == 1
        finally:
            await c.shutdown()
            await c.shutdown()
        assert provider.closed == h.embedding.closed == 1
        assert counts == dict(clients=1, requests=len(cases), closed=1)
        print('speech.desktop.routing=PASS;production_playback=true;physical_device=controlled;db_speech_sessions=0;audio_retained=0')
        print('speech.desktop.routing.qwen=' + json.dumps(counts))
        print('speech.desktop.routing.tts=' + json.dumps(dict(clients=provider.clients, closed=provider.closed,
            pl_requests=provider.requests[SpeechLanguage.PL], en_requests=provider.requests[SpeechLanguage.EN])))
    asyncio.run(asyncio.wait_for(scenario(), 600))
