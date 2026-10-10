"""Actual diagnostic CLI with real planner/voices and production playback."""
import os

import pytest
from backend.tests.polish_english_fakes import CompleteAudioDevice, PL_EN_CASES
from kulai_memory import speech_runtime
from kulai_memory.application.speech import SpeechLanguage
from scripts import tts_smoke as cli


@pytest.mark.parametrize('case', [14, 11], ids=['pl_en_pl', 'en_pl_en'])
def test_real_cli_planner_synthesis_production_playback_cleanup(monkeypatch, capsys, case):
    if not all(os.environ.get(name) == '1' for name in ('KULAI_RUN_LLM_INTEGRATION', 'KULAI_RUN_TTS_INTEGRATION')):
        pytest.skip('Enable local Qwen and installed Windows PL/EN voices for diagnostic CLI integration.')
    if os.name != 'nt':
        pytest.skip('System.Speech requires Windows.')
    monkeypatch.setenv('KULAI_TTS_ENABLED', 'true')
    monkeypatch.setenv('KULAI_TTS_PL_VOICE', 'Microsoft Paulina Desktop')
    monkeypatch.setenv('KULAI_TTS_EN_VOICE', 'Microsoft Zira Desktop')
    device = CompleteAudioDevice()
    original_player = speech_runtime.SoundDevicePlayback
    monkeypatch.setattr(speech_runtime, 'SoundDevicePlayback', lambda: original_player(audio_module=device))
    original_factory = cli.create_speech_runtime
    runtimes = []
    def create(**kwargs):
        runtime = original_factory(**kwargs)
        runtimes.append(runtime)
        return runtime
    monkeypatch.setattr(cli, 'create_speech_runtime', create)
    answer, languages, _ = PL_EN_CASES[case]
    assert cli.main(['--text', answer]) == 0  # No --no-play: full production playback.
    output = capsys.readouterr()
    assert not output.err and answer not in output.out
    lines = output.out.splitlines()
    assert lines[0] == 'segment_count=3' and len(lines) == 4
    for number, language in enumerate(languages, 1):
        voice = 'Microsoft Paulina Desktop' if language == 'pl' else 'Microsoft Zira Desktop'
        assert lines[number].startswith(f'segment={number} language={language} chars=')
        assert lines[number].endswith(f'voice={voice}')
    assert len(runtimes) == 1
    runtime = runtimes[0]
    assert runtime.provider.clients == runtime.provider.closed == 1 and not runtime.artifacts
    assert runtime._directory is None and runtime.playback._work is runtime.playback._stream is None
    assert runtime.provider.requests == {lang: languages.count(lang.value) for lang in SpeechLanguage}
    assert len(device.streams) == 3 and device.maximum_active == 1 and device.active == 0
    assert all(stream.output and stream.closed == stream.aborted == 1 for stream in device.streams)
    print(output.out, end='')  # Only permitted metadata, no answer/audio/payload.
