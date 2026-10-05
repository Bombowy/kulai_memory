from __future__ import annotations

import tomllib
from pathlib import Path

from kulai_provider_whisper import WhisperTranscriptionProvider

from kulai_memory.settings import Settings
from kulai_memory.whisper_provider import create_whisper_transcription_provider

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_factory_builds_configured_concrete_provider() -> None:
    settings = Settings(
        _env_file=None,
        kulai_whisper_model="large-v3",
        kulai_whisper_device="cuda",
        kulai_whisper_compute_type="int8_float16",
    )

    provider = create_whisper_transcription_provider(settings=settings)

    assert isinstance(provider, WhisperTranscriptionProvider)
    assert provider.config.model_size_or_path == "large-v3"
    assert provider.config.device == "cuda"
    assert provider.config.compute_type == "int8_float16"


def test_host_declares_direct_stt_dependencies_and_pyav_compatibility() -> None:
    metadata = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text("utf-8"))
    dependencies = metadata["project"]["dependencies"]

    assert any(item.startswith("kulai-transcription") for item in dependencies)
    assert any(item.startswith("kulai-provider-whisper") for item in dependencies)
    assert "av>=11,<19" in dependencies
