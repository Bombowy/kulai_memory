from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import wave
from pathlib import Path

import pytest
from kulai_transcription import (
    AudioPathInput,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionTimestampMode,
)

from kulai_memory.application import (
    TranscriptFinalEvent,
    TranscriptionService,
    VoiceSession,
    VoiceSessionEvent,
)
from kulai_memory.whisper_provider import create_whisper_transcription_provider

pytestmark = pytest.mark.skipif(
    os.environ.get("KULAI_RUN_WHISPER_INTEGRATION") != "1",
    reason="set KULAI_RUN_WHISPER_INTEGRATION=1 to run real Whisper inference",
)


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[VoiceSessionEvent] = []

    async def emit(self, event: VoiceSessionEvent) -> None:
        self.events.append(event)


def _write_silence(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(bytes(16_000 * 2))


def test_real_whisper_host_flow_emits_final_event() -> None:
    async def scenario(audio_path: Path) -> None:
        sink = _CollectingSink()
        provider = create_whisper_transcription_provider()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=provider),
        )
        request = TranscriptionRequest(
            audio=AudioPathInput(path=audio_path),
            mode=TranscriptionMode.TRANSCRIBE,
            timestamp_mode=TranscriptionTimestampMode.NONE,
        )

        await session.start()
        result = await session.transcribe(request=request)
        await session.close()

        assert isinstance(result, TranscriptionResult)
        finals = [event for event in sink.events if isinstance(event, TranscriptFinalEvent)]
        assert len(finals) == 1
        assert [event.sequence for event in sink.events] == [1, 2]
        assert hashlib.sha256(finals[0].payload.text.encode()).digest() == hashlib.sha256(
            result.text.encode()
        ).digest()

    configured_audio = os.environ.get("KULAI_WHISPER_AUDIO")
    if configured_audio:
        asyncio.run(scenario(Path(configured_audio)))
        return

    with tempfile.TemporaryDirectory(prefix="kulai-whisper-integration-") as temp_dir:
        audio_path = Path(temp_dir) / "silence.wav"
        _write_silence(audio_path)
        asyncio.run(scenario(audio_path))
