from __future__ import annotations

import asyncio
import math
import os
import random
import struct
import wave
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from kulai_provider_whisper import WhisperTranscriptionProvider
from kulai_transcription import (
    AudioPathInput,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionTimestampMode,
)

from kulai_memory.application import (
    IdempotentMemoryWrite,
    Memory,
    MemoryService,
    TranscriptFinalEvent,
    TranscriptMemoryIngestionService,
    TranscriptMemoryIngestionStatus,
    TranscriptionService,
    VoiceSession,
    VoiceSessionEvent,
)
from kulai_memory.settings import Settings
from kulai_memory.whisper_provider import create_whisper_transcription_provider

pytestmark = pytest.mark.skipif(
    os.environ.get("KULAI_RUN_WHISPER_INTEGRATION") != "1",
    reason="set KULAI_RUN_WHISPER_INTEGRATION=1 to run real Whisper inference",
)

_SAMPLE_RATE = 16_000


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[VoiceSessionEvent] = []

    async def emit(self, event: VoiceSessionEvent) -> None:
        self.events.append(event)


class _RejectingMemoryRepository:
    async def create(self, memory: Memory) -> Memory:
        del memory
        raise AssertionError("empty transcript must not create Memory")

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        del memory
        raise AssertionError("empty transcript must not create Memory")

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        del memory_id
        raise AssertionError("empty transcript must not read Memory")

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        del limit
        raise AssertionError("empty transcript must not list Memory")


@pytest.fixture(scope="module")
def real_provider() -> WhisperTranscriptionProvider:
    settings = Settings(
        _env_file=None,
        kulai_whisper_model="large-v3",
        kulai_whisper_device="cuda",
        kulai_whisper_compute_type="int8_float16",
        kulai_whisper_vad_filter=True,
    )
    provider = create_whisper_transcription_provider(settings=settings)
    assert isinstance(provider, WhisperTranscriptionProvider)
    assert provider.config.model_size_or_path == "large-v3"
    assert provider.config.device == "cuda"
    assert provider.config.compute_type == "int8_float16"
    assert provider.config.vad_filter is True
    return provider


def _write_pcm(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(_SAMPLE_RATE)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _negative_audio_fixtures(root: Path) -> dict[str, Path]:
    sample_sets: dict[str, list[int]] = {
        "silence_1s": [0] * _SAMPLE_RATE,
        "silence_3s": [0] * (3 * _SAMPLE_RATE),
    }

    click = [0] * _SAMPLE_RATE
    click[_SAMPLE_RATE // 2] = 32767
    sample_sets["click"] = click

    random_source = random.Random(0x3A2)
    sample_sets["noise"] = [
        random_source.randint(-256, 256) for _ in range(3 * _SAMPLE_RATE)
    ]
    sample_sets["tone"] = [
        round(4096 * math.sin(2 * math.pi * 440 * index / _SAMPLE_RATE))
        for index in range(3 * _SAMPLE_RATE)
    ]

    paths: dict[str, Path] = {}
    for name, samples in sample_sets.items():
        path = root / f"{name}.wav"
        _write_pcm(path, samples)
        paths[name] = path
    return paths


def _safe_metrics(label: str, result: TranscriptionResult) -> str:
    language = result.language.code if result.language is not None else "unknown"
    duration = (
        str(result.duration_seconds)
        if result.duration_seconds is not None
        else "unknown"
    )
    nonempty_segments = sum(
        1 for segment in result.segments if segment.text.strip()
    )
    return (
        f"fixture={label} char_count={len(result.text)} "
        f"segment_count={len(result.segments)} "
        f"nonempty_segment_count={nonempty_segments} "
        f"language={language} duration={duration}"
    )


def _request(path: Path, *, language_hint: str | None = None) -> TranscriptionRequest:
    return TranscriptionRequest(
        audio=AudioPathInput(path=path),
        language_hint=language_hint,
        mode=TranscriptionMode.TRANSCRIBE,
        timestamp_mode=TranscriptionTimestampMode.NONE,
    )


def test_real_whisper_host_flow_emits_empty_final_for_silence(
    real_provider: WhisperTranscriptionProvider,
    tmp_path: Path,
) -> None:
    async def scenario(audio_path: Path) -> None:
        sink = _CollectingSink()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=real_provider),
        )

        await session.start()
        result = await session.transcribe(request=_request(audio_path))
        ingestion = await TranscriptMemoryIngestionService(
            memory_service=MemoryService(repository=_RejectingMemoryRepository())
        ).ingest(
            transcription=result,
            ingestion_id=uuid4(),
            session_id=session.session_id,
        )
        await session.close()

        assert isinstance(result, TranscriptionResult)
        assert result.text.strip() == ""
        assert not any(segment.text.strip() for segment in result.segments)
        assert ingestion.status is TranscriptMemoryIngestionStatus.SKIPPED_EMPTY
        assert ingestion.memory is None
        finals = [event for event in sink.events if isinstance(event, TranscriptFinalEvent)]
        assert len(finals) == 1
        assert finals[0].payload.text == ""
        assert [event.sequence for event in sink.events] == [1, 2]

    audio_path = tmp_path / "silence_1s.wav"
    _write_pcm(audio_path, [0] * _SAMPLE_RATE)
    asyncio.run(scenario(audio_path))


def test_real_whisper_vad_rejects_negative_audio_matrix_with_one_model(
    real_provider: WhisperTranscriptionProvider,
    tmp_path: Path,
) -> None:
    paths = _negative_audio_fixtures(tmp_path)
    cases = [
        ("silence_1s", None),
        ("silence_3s", None),
        ("click", None),
        ("noise", None),
        ("tone", None),
        ("silence_1s", "pl"),
        ("silence_3s", "pl"),
    ]

    async def scenario() -> None:
        loaded_model = None
        for fixture_name, language_hint in cases:
            label = (
                fixture_name
                if language_hint is None
                else f"{fixture_name}_language_{language_hint}"
            )
            result = await real_provider.transcribe(
                _request(paths[fixture_name], language_hint=language_hint)
            )
            if loaded_model is None:
                loaded_model = getattr(real_provider, "_model", None)

            metrics = _safe_metrics(label, result)
            print(f"negative_audio {metrics}")
            assert result.text.strip() == "", metrics
            assert not any(segment.text.strip() for segment in result.segments), metrics

        assert loaded_model is not None
        assert getattr(real_provider, "_model", None) is loaded_model
        print("negative_audio model_reused=true")

    asyncio.run(scenario())


def test_optional_real_speech_remains_nonempty(
    real_provider: WhisperTranscriptionProvider,
) -> None:
    configured_audio = os.environ.get("KULAI_WHISPER_AUDIO")
    if not configured_audio:
        pytest.skip("KULAI_WHISPER_AUDIO is not set")

    async def scenario() -> None:
        result = await real_provider.transcribe(_request(Path(configured_audio)))
        metrics = _safe_metrics("explicit_speech", result)
        print(f"positive_audio {metrics}")
        assert result.text.strip() != "", metrics
        assert any(segment.text.strip() for segment in result.segments), metrics

    asyncio.run(scenario())
