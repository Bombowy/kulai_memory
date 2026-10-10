from __future__ import annotations
import asyncio
import traceback
from pathlib import Path

import pytest
from backend.tests.speech_fakes import FakeSpeechProvider, FakePlayback, speech_plan, wav_bytes
from kulai_memory.application.speech import SpeechError, SpeechLanguage
from kulai_memory.speech_runtime import SpeechRuntime
from kulai_memory.audio_playback import read_wave


def test_routes_in_order_reuses_one_provider_and_cleans_all_waves():
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        runtime = SpeechRuntime(provider=provider, playback=player)
        plan = speech_plan()
        for _ in range(2):
            await runtime.speak(plan)
            assert not runtime.artifacts and runtime._directory is None
        assert provider.calls == list(plan.segments) * 2
        assert [s.language for s in provider.calls] == [SpeechLanguage.PL, SpeechLanguage.EN, SpeechLanguage.PL] * 2
        assert len(player.paths) == 6 and len(set(player.paths)) == 6
        assert all(not path.exists() and not path.parent.exists() for path in player.paths)
        assert all(not path.is_relative_to(Path.cwd()) for path in player.paths)
        await runtime.aclose()
        await runtime.aclose()
        assert provider.closed == 1
    asyncio.run(scenario())


def test_optional_diagnostics_contain_only_segment_routing_metadata():
    from dataclasses import asdict
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        runtime = SpeechRuntime(provider=provider, playback=player)
        infos = []
        plan = speech_plan()
        try:
            await runtime.speak(plan, on_segment=infos.append)
            assert [asdict(info) for info in infos] == [dict(number=i, language=segment.language,
                character_count=len(segment.text), voice_id='voice-' + segment.language.value)
                for i, segment in enumerate(plan.segments, 1)]
            assert plan.original_answer not in repr(infos)
            assert not runtime.artifacts
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['segment2', 'playback', 'format', 'language'])
def test_failures_stop_queue_clean_prior_artifacts_and_are_safe(failure):
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        runtime = SpeechRuntime(provider=provider, playback=player)
        if failure == 'segment2':
            provider.error_at = 2
        elif failure == 'playback':
            player.error = True
        else:
            original = provider.synthesize
            async def wrong(segment):
                from dataclasses import replace
                result = await original(segment)
                return replace(result, audio=b'PRIVATE_INVALID_AUDIO') if failure == 'format' else replace(result, language=SpeechLanguage.EN)
            provider.synthesize = wrong
        with pytest.raises(SpeechError) as caught:
            await runtime.speak(speech_plan())
        assert 'PRIVATE' not in ''.join(traceback.format_exception(caught.value))
        assert not runtime.artifacts and runtime._directory is None
        assert all(not p.exists() for p in player.paths)
        if failure == 'segment2':
            assert len(provider.calls) == 2 and not player.paths
        if failure == 'playback':
            assert len(player.paths) == 1
        await runtime.aclose()
        assert provider.closed == 1
    asyncio.run(scenario())


def test_stop_cancels_playback_cleans_then_next_speak_reuses_provider():
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        player.release.clear()
        runtime = SpeechRuntime(provider=provider, playback=player)
        task = asyncio.create_task(runtime.speak(speech_plan()))
        await player.entered.wait()
        paths = runtime.artifacts
        assert len(paths) == 3 and all(p.exists() for p in paths)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not p.exists() for p in paths) and provider.closed == 0
        player.release.set()
        await runtime.speak(speech_plan())
        await runtime.aclose()
        assert provider.closed == 1 and not runtime.artifacts
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize('audio', [b'bad', b'RIFF' + b'x' * 80, wav_bytes(width=1)])
def test_only_complete_pcm16_wav_is_accepted(audio):
    with pytest.raises(SpeechError):
        read_wave(audio)


@pytest.mark.parametrize('rate,channels', [(22050, 1), (48000, 2)])
def test_wav_metadata_keeps_sample_rate_and_channels(rate, channels):
    audio = read_wave(wav_bytes(rate=rate, channels=channels))
    assert audio.sample_rate == rate and audio.channels == channels and audio.duration > 0


@pytest.mark.parametrize('phase', ['directory', 'write'])
def test_cancel_drains_file_work_then_cleans_every_owned_path(monkeypatch, phase):
    import threading
    from kulai_memory import speech_runtime
    entered, release, created = threading.Event(), threading.Event(), []
    if phase == 'directory':
        original = speech_runtime.tempfile.mkdtemp
        def make(*args, **kwargs):
            directory = original(*args, **kwargs)
            created.append(Path(directory))
            entered.set()
            assert release.wait(3)
            return directory
        monkeypatch.setattr(speech_runtime.tempfile, 'mkdtemp', make)
    else:
        original = Path.open
        def open_file(path, *args, **kwargs):
            if args and args[0] == 'xb' and path.name.startswith('segment_'):
                created.append(path)
                entered.set()
                assert release.wait(3)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', open_file)
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        runtime = SpeechRuntime(provider=provider, playback=player)
        task = asyncio.create_task(runtime.speak(speech_plan()))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert created and all(not path.exists() for path in created)
        assert not runtime.artifacts and runtime._directory is None and not player.paths
        await runtime.aclose()
    try:
        asyncio.run(asyncio.wait_for(scenario(), 5))
    finally:
        release.set()


def test_runtime_close_active_playback_cancels_cleans_and_closes_once():
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        player.release.clear()
        runtime = SpeechRuntime(provider=provider, playback=player)
        task = asyncio.create_task(runtime.speak(speech_plan()))
        await player.entered.wait()
        paths = runtime.artifacts
        await runtime.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runtime.aclose()
        assert provider.closed == 1 and all(not p.exists() for p in paths) and not runtime.artifacts
    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_whole_speech_operation_timeout_cleans_safe(monkeypatch):
    from kulai_memory import speech_runtime
    monkeypatch.setattr(speech_runtime, 'SPEECH_OPERATION_TIMEOUT_SECONDS', .02)
    async def scenario():
        provider, player = FakeSpeechProvider(), FakePlayback()
        player.release.clear()
        runtime = SpeechRuntime(provider=provider, playback=player)
        with pytest.raises(SpeechError, match='^Could not speak answer\\.$'):
            await runtime.speak(speech_plan())
        assert not runtime.artifacts and all(not p.exists() for p in player.paths)
        await runtime.aclose()
    asyncio.run(asyncio.wait_for(scenario(), 1))
