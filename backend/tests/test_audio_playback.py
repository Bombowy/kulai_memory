from __future__ import annotations

import asyncio
import threading

import pytest

from backend.tests.speech_fakes import wav_bytes
from kulai_memory.audio_playback import SoundDevicePlayback, read_wave
from kulai_memory.application.speech import SpeechError


class FakeAudio:
    class CallbackStop(Exception):
        pass

    def __init__(self, *, blocked=False, failure=None):
        self.streams = []
        self.blocked, self.failure = blocked, failure
        self.entered = threading.Event()

    def RawOutputStream(self, **kwargs):
        owner = self
        class Stream:
            aborted = closed = 0
            output = None
            def start(self):
                owner.entered.set()
                if owner.failure == 'start':
                    raise RuntimeError('PRIVATE_DEVICE_PAYLOAD')
                if not owner.blocked:
                    self.output = bytearray(1024 * kwargs['channels'] * 2)
                    try:
                        kwargs['callback'](self.output, 1024, None, None)
                    except owner.CallbackStop:
                        kwargs['finished_callback']()
            def abort(self):
                self.aborted += 1
            def close(self):
                self.closed += 1
        stream = Stream()
        stream.parameters = kwargs
        self.streams.append(stream)
        return stream


def test_pcm_callback_end_padding_order_and_private_streams(tmp_path):
    async def scenario():
        module = FakeAudio()
        player = SoundDevicePlayback(audio_module=module)
        for rate, channels in ((16000, 1), (22050, 2)):
            path = tmp_path / f'{rate}.wav'
            audio = wav_bytes(rate=rate, channels=channels)
            path.write_bytes(audio)
            await player.play(path)
            stream = module.streams[-1]
            assert stream.parameters['samplerate'] == rate and stream.parameters['channels'] == channels
            pcm = read_wave(audio).pcm
            assert stream.output == pcm + bytes(len(stream.output) - len(pcm))
            assert stream.aborted == stream.closed == 1 and path.exists()
        assert len(module.streams) == 2 and player._work is None
        await player.stop()
    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_stop_and_cancellation_drain_native_stream_then_next_play_works(tmp_path):
    async def scenario():
        path = tmp_path / 'audio.wav'
        path.write_bytes(wav_bytes())
        module = FakeAudio(blocked=True)
        player = SoundDevicePlayback(audio_module=module)
        task = asyncio.create_task(player.play(path))
        await asyncio.to_thread(module.entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert module.streams[0].aborted == module.streams[0].closed == 1
        assert player._work is player._stream is None and path.exists()
        module.blocked = False
        await player.play(path)
        assert module.streams[1].closed == 1
    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_native_start_failure_closes_and_hides_payload(tmp_path):
    async def scenario():
        path = tmp_path / 'audio.wav'
        path.write_bytes(wav_bytes())
        module = FakeAudio(failure='start')
        with pytest.raises(SpeechError, match='^Could not speak answer\\.$'):
            await SoundDevicePlayback(audio_module=module).play(path)
        assert module.streams[0].closed == 1
    asyncio.run(scenario())
