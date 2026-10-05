from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from kulai_transcription import (
    AudioInputKind,
    TranscriptionCapabilities,
    TranscriptionResult,
)

from scripts import stt_smoke


class _FakeProvider:
    provider_id = "fake-whisper"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.PATH})
    )

    def __init__(self) -> None:
        self._model = object()

    async def transcribe(self, _request) -> TranscriptionResult:
        return TranscriptionResult(
            text="synthetic transcript content",
            provider_id=self.provider_id,
            model_id="large-v3",
            duration_seconds=1.0,
        )


def test_cache_detection_is_best_effort_without_huggingface_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(name: str):
        assert name == "huggingface_hub"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(stt_smoke.importlib, "import_module", unavailable)

    assert stt_smoke._is_cached("large-v3") is None


def test_smoke_reports_vad_and_hides_synthetic_transcript(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        kulai_whisper_model="large-v3",
        kulai_whisper_device="cuda",
        kulai_whisper_compute_type="int8_float16",
        kulai_whisper_vad_filter=True,
    )
    provider = _FakeProvider()
    monkeypatch.setattr(stt_smoke, "get_settings", lambda: settings)
    monkeypatch.setattr(
        stt_smoke,
        "create_whisper_transcription_provider",
        lambda *, settings: provider,
    )
    monkeypatch.setattr(stt_smoke, "_is_cached", lambda _model: None)

    asyncio.run(
        stt_smoke._run(
            tmp_path / "synthetic.wav",
            language=None,
            show_text=False,
        )
    )

    output = capsys.readouterr().out
    assert "vad_filter=true" in output
    assert "transcript_character_count=28" in output
    assert "synthetic transcript content" not in output
    assert "transcript:" not in output


def test_smoke_may_show_transcript_for_explicit_audio(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        kulai_whisper_model="large-v3",
        kulai_whisper_device="cuda",
        kulai_whisper_compute_type="int8_float16",
        kulai_whisper_vad_filter=False,
    )
    provider = _FakeProvider()
    monkeypatch.setattr(stt_smoke, "get_settings", lambda: settings)
    monkeypatch.setattr(
        stt_smoke,
        "create_whisper_transcription_provider",
        lambda *, settings: provider,
    )
    monkeypatch.setattr(stt_smoke, "_is_cached", lambda _model: None)

    asyncio.run(
        stt_smoke._run(
            tmp_path / "explicit.wav",
            language=None,
            show_text=True,
        )
    )

    output = capsys.readouterr().out
    assert "vad_filter=false" in output
    assert "transcript:\nsynthetic transcript content" in output
