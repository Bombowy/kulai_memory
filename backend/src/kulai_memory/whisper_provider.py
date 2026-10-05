"""Host-specific construction of the concrete Whisper provider."""

from __future__ import annotations

from kulai_provider_whisper import WhisperProviderConfig, WhisperTranscriptionProvider
from kulai_transcription import TranscriptionProvider

from .settings import Settings, get_settings


def create_whisper_transcription_provider(
    *, settings: Settings | None = None
) -> TranscriptionProvider:
    """Create one reusable provider instance from host configuration."""

    active_settings = settings or get_settings()
    config = WhisperProviderConfig(
        model_size_or_path=active_settings.kulai_whisper_model,
        device=active_settings.kulai_whisper_device,
        compute_type=active_settings.kulai_whisper_compute_type,
        vad_filter=active_settings.kulai_whisper_vad_filter,
    )
    return WhisperTranscriptionProvider(config=config)
