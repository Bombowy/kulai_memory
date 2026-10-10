"""Ephemeral speech synthesis/playback ownership; no database or LLM clients."""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .application.speech import SpeechError, SpeechLanguage, SpeechPlan, SpeechProvider, SynthesizedSpeech
from .audio_playback import SoundDevicePlayback, read_wave
from .local_tts import WindowsSpeechProvider, SYNTHESIS_TIMEOUT_SECONDS, drain_audio_work
from .settings import Settings

SPEECH_OPERATION_TIMEOUT_SECONDS = 600.0
MAX_OPERATION_AUDIO_BYTES = 64 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AudioPlayback(Protocol):
    async def play(self, path: Path) -> None: ...
    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SpeechSegmentInfo:
    """Optional diagnostic metadata, containing neither answer nor audio."""
    number: int
    language: SpeechLanguage
    character_count: int
    voice_id: str


class SpeechRuntime:
    """Own one long-lived provider and one player, and every private WAV it creates."""
    def __init__(self, *, provider: SpeechProvider, playback: AudioPlayback):
        self.provider, self.playback = provider, playback
        self._closed = False
        self._active = False
        self._artifacts: list[Path] = []
        self._directory: Path | None = None
        self._task: asyncio.Task | None = None

    @property
    def artifacts(self) -> tuple[Path, ...]:
        return tuple(self._artifacts)

    async def prepare(self) -> None:
        await self.provider.prepare()

    async def speak(self, plan: SpeechPlan, *, on_speaking: Callable[[], None] | None = None,
                    on_segment: Callable[[SpeechSegmentInfo], None] | None = None):
        try:
            async with asyncio.timeout(SPEECH_OPERATION_TIMEOUT_SECONDS):
                await self._speak(plan, on_speaking=on_speaking, on_segment=on_segment)
        except Exception:
            raise SpeechError() from None

    async def _speak(self, plan: SpeechPlan, *, on_speaking: Callable[[], None] | None,
                     on_segment: Callable[[SpeechSegmentInfo], None] | None):
        if self._closed or self._active or not isinstance(plan, SpeechPlan):
            raise SpeechError()
        self._active = True
        self._task = asyncio.current_task()
        try:
            def create_directory():
                root = Path(tempfile.gettempdir()).resolve()
                if root.is_relative_to(PROJECT_ROOT):
                    raise SpeechError()
                self._directory = Path(tempfile.mkdtemp(prefix="kulai_speech_", dir=root))
            await drain_audio_work(asyncio.create_task(asyncio.to_thread(create_directory)))
            total_bytes = total_duration = 0
            for number, segment in enumerate(plan.segments, 1):
                speech = await asyncio.wait_for(self.provider.synthesize(segment), SYNTHESIS_TIMEOUT_SECONDS)
                if not isinstance(speech, SynthesizedSpeech) or speech.language is not segment.language or not speech.voice_id:
                    raise SpeechError()
                wave = await asyncio.to_thread(read_wave, speech.audio)
                total_bytes += len(speech.audio)
                total_duration += wave.duration
                if total_bytes > MAX_OPERATION_AUDIO_BYTES or total_duration > 600:
                    raise SpeechError()
                if on_segment is not None:
                    on_segment(SpeechSegmentInfo(number, segment.language, len(segment.text), speech.voice_id))
                path = self._directory / f"segment_{len(self._artifacts):02d}.wav"
                self._artifacts.append(path)  # Track ownership before starting the write.
                def write_audio():
                    with path.open("xb") as handle:
                        handle.write(speech.audio)
                await drain_audio_work(asyncio.create_task(asyncio.to_thread(write_audio)))
            if total_duration == 0:
                raise SpeechError()
            if on_speaking is not None:
                on_speaking()
            for path in self._artifacts:
                await self.playback.play(path)
        except Exception:
            raise SpeechError() from None
        finally:
            async def cleanup():
                try:
                    await self.playback.stop()
                finally:
                    def remove_owned():
                        for path in self._artifacts:
                            path.unlink(missing_ok=True)
                        if self._directory is not None:
                            self._directory.rmdir()  # Never recursively delete or remove unowned paths.
                    await asyncio.to_thread(remove_owned)
                    self._artifacts.clear()
                    self._directory = None
            try:
                await drain_audio_work(asyncio.create_task(cleanup()))
            except Exception:
                raise SpeechError() from None
            finally:
                self._active = False
                self._task = None

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            async def wait():
                await asyncio.gather(task, return_exceptions=True)
            await drain_audio_work(asyncio.create_task(wait()))
        try:
            await self.playback.stop()
        finally:
            await self.provider.aclose()


def create_speech_runtime(*, settings: Settings) -> SpeechRuntime | None:
    if not settings.kulai_tts_enabled:
        return None
    return SpeechRuntime(provider=WindowsSpeechProvider(pl_voice=settings.kulai_tts_pl_voice,
        en_voice=settings.kulai_tts_en_voice), playback=SoundDevicePlayback())
