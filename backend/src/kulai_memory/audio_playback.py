"""Ordered local PCM playback on the asyncio worker, without Qt or global sd.play."""
from __future__ import annotations

import asyncio
import io
import wave
from dataclasses import dataclass
from pathlib import Path

from .application.speech import SpeechError
from .local_tts import MAX_AUDIO_BYTES, drain_audio_work


@dataclass(frozen=True, slots=True, repr=False)
class WaveAudio:
    pcm: bytes
    sample_rate: int
    channels: int
    frames: int

    @property
    def duration(self):
        return self.frames / self.sample_rate


def read_wave(audio: bytes) -> WaveAudio:
    try:
        if not isinstance(audio, bytes) or not 44 <= len(audio) <= MAX_AUDIO_BYTES:
            raise SpeechError()
        with wave.open(io.BytesIO(audio), "rb") as source:
            rate, channels, frames = source.getframerate(), source.getnchannels(), source.getnframes()
            if (source.getcomptype() != "NONE" or source.getsampwidth() != 2
                    or channels not in (1, 2) or not 8000 <= rate <= 96000 or frames / rate > 600):
                raise SpeechError()
            pcm = source.readframes(frames)
            if len(pcm) != frames * channels * 2:
                raise SpeechError()
        return WaveAudio(pcm, rate, channels, frames)
    except Exception:
        raise SpeechError() from None


class SoundDevicePlayback:
    """A private RawOutputStream per WAV; native completion precedes file cleanup."""
    def __init__(self, *, audio_module=None):
        self._audio_module = audio_module
        self._work: asyncio.Task | None = None
        self._stream = None
        self._finished: asyncio.Event | None = None
        self._stopping = False

    async def play(self, path: Path):
        if self._work is not None:
            raise SpeechError()
        self._stopping = False
        self._finished = asyncio.Event()
        self._work = asyncio.create_task(self._play(path))
        try:
            await asyncio.shield(self._work)
        except asyncio.CancelledError:
            await drain_audio_work(asyncio.create_task(self.stop()))
            raise
        finally:
            self._work = None
            self._finished = None

    async def _play(self, path):
        module = self._audio_module
        if module is None:
            import sounddevice as module
        # File IO and parsing are offloaded; the file is closed before playback starts.
        audio = await asyncio.to_thread(lambda: read_wave(path.read_bytes()))
        if self._stopping or audio.frames == 0:
            return
        loop = asyncio.get_running_loop()
        finished = self._finished
        position = 0
        def callback(output, frames, time_info, status):
            nonlocal position
            size = frames * audio.channels * 2
            chunk = audio.pcm[position:position + size]
            output[:] = chunk + bytes(size - len(chunk))
            position += len(chunk)
            if position >= len(audio.pcm) or self._stopping:
                raise module.CallbackStop
        def completed():
            loop.call_soon_threadsafe(finished.set)
        def open_stream():
            stream = module.RawOutputStream(samplerate=audio.sample_rate, channels=audio.channels,
                dtype="int16", callback=callback, finished_callback=completed)
            try:
                stream.start()
                return stream
            except BaseException:
                stream.close()
                raise
        def close_stream(stream):
            try:
                stream.abort()
            finally:
                stream.close()
        try:
            self._stream = await asyncio.to_thread(open_stream)
            if not self._stopping:
                await asyncio.wait_for(finished.wait(), timeout=audio.duration + 10.0)
        except Exception:
            raise SpeechError() from None
        finally:
            stream, self._stream = self._stream, None
            if stream is not None:
                await drain_audio_work(asyncio.create_task(asyncio.to_thread(close_stream, stream)))

    async def stop(self):
        self._stopping = True
        if self._finished is not None:
            self._finished.set()
        work = self._work
        if work is not None and work is not asyncio.current_task():
            # _play closes/aborts its stream before returning. Never unlink a read in flight.
            await drain_audio_work(work)
