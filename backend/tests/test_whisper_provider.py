from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from kulai_provider_whisper import WhisperTranscriptionProvider

from kulai_memory.settings import Settings
from kulai_memory.whisper_provider import create_whisper_transcription_provider

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
VENDOR_PROVIDER_METADATA = (
    PROJECT_ROOT
    / "vendor"
    / "kulai_modules"
    / "kulai_provider_whisper"
    / "pyproject.toml"
)


def _dependency_names(requirements: list[str]) -> set[str]:
    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, f"Invalid dependency requirement: {requirement!r}"
        names.add(match.group(0).lower().replace("_", "-"))
    return names


def test_host_whisper_defaults_are_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "KULAI_WHISPER_MODEL",
        "KULAI_WHISPER_DEVICE",
        "KULAI_WHISPER_COMPUTE_TYPE",
        "KULAI_WHISPER_VAD_FILTER",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.kulai_whisper_model == "large-v3"
    assert settings.kulai_whisper_device == "cuda"
    assert settings.kulai_whisper_compute_type == "int8_float16"
    assert settings.kulai_whisper_vad_filter is True


def test_host_vad_can_be_disabled_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KULAI_WHISPER_VAD_FILTER", "false")

    settings = Settings(_env_file=None)

    assert settings.kulai_whisper_vad_filter is False


@pytest.mark.parametrize("vad_filter", [True, False])
def test_factory_builds_configured_concrete_provider(vad_filter: bool) -> None:
    settings = Settings(
        _env_file=None,
        kulai_whisper_model="large-v3",
        kulai_whisper_device="cuda",
        kulai_whisper_compute_type="int8_float16",
        kulai_whisper_vad_filter=vad_filter,
    )

    provider = create_whisper_transcription_provider(settings=settings)

    assert isinstance(provider, WhisperTranscriptionProvider)
    assert provider.config.model_size_or_path == "large-v3"
    assert provider.config.device == "cuda"
    assert provider.config.compute_type == "int8_float16"
    assert provider.config.vad_filter is vad_filter


def test_provider_owns_pyav_compatibility_contract() -> None:
    host_metadata = tomllib.loads(
        (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    provider_metadata = tomllib.loads(
        VENDOR_PROVIDER_METADATA.read_text(encoding="utf-8")
    )
    host_dependencies = host_metadata["project"]["dependencies"]
    provider_dependencies = provider_metadata["project"]["dependencies"]
    host_names = _dependency_names(host_dependencies)

    assert "kulai-transcription" in host_names
    assert "kulai-provider-whisper" in host_names
    assert "av" not in host_names
    assert "av>=11,<19" in provider_dependencies
