from __future__ import annotations

import asyncio
import os
import shutil
import struct
import wave
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from kulai_memory.database_safety import (
    async_database_url,
    create_owned_temporary_database,
    database_config,
    database_config_for_database,
    database_host_is_loopback,
    drop_owned_temporary_database,
)
from kulai_memory.desktop.controller import DesktopController
from kulai_memory.desktop.models import (
    DesktopResultStatus,
    MicrophoneDevice,
    RecordingArtifact,
)
from kulai_memory.settings import Settings, get_settings
from scripts import migrate


pytestmark = pytest.mark.skipif(
    os.environ.get("KULAI_RUN_POSTGRES_INTEGRATION") != "1"
    or os.environ.get("KULAI_RUN_WHISPER_INTEGRATION") != "1",
    reason=(
        "set KULAI_RUN_POSTGRES_INTEGRATION=1 and "
        "KULAI_RUN_WHISPER_INTEGRATION=1"
    ),
)


def _require_safe_environment() -> None:
    settings = get_settings()
    if settings.app_env.lower() not in {"dev", "development", "local", "test"}:
        pytest.fail("Desktop integration requires a non-production APP_ENV.")
    if not database_host_is_loopback(database_config()):
        pytest.fail("Desktop integration requires a loopback PostgreSQL database.")


def _upgrade_database(url: str) -> None:
    previous_url = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        config = migrate.alembic_config()
        assert migrate.configure_x_arguments(config)
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()


def _write_silence(path: Path) -> None:
    samples = [0] * 16_000
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


class _QueuedRecorder:
    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        self._active = False
        self.cleaned: list[Path] = []

    def list_devices(self) -> tuple[MicrophoneDevice, ...]:
        return (MicrophoneDevice(0, "Integration fixture", None, True),)

    def start(self, *, device_id: int) -> None:
        assert device_id == 0 and not self._active
        self._active = True

    def stop(self) -> RecordingArtifact:
        assert self._active and self._paths
        self._active = False
        return RecordingArtifact(
            path=self._paths.pop(0),
            duration_seconds=1.0,
            limit_reached=False,
        )

    def cleanup(self, artifact: RecordingArtifact) -> None:
        artifact.path.unlink()
        self.cleaned.append(artifact.path)

    def shutdown(self) -> None:
        for path in self._paths:
            path.unlink(missing_ok=True)
        self._paths.clear()


def test_real_desktop_runtime_reuses_large_v3_and_skips_silence(
    tmp_path: Path,
) -> None:
    _require_safe_environment()

    async def scenario() -> None:
        main_config = database_config()
        owned = await create_owned_temporary_database(kind="backup", config=main_config)
        owned_url = async_database_url(database=owned.name, config=main_config)
        controller: DesktopController | None = None
        provider: Any = None
        try:
            await asyncio.to_thread(_upgrade_database, owned_url)
            settings = Settings()
            assert settings.kulai_whisper_model == "large-v3"
            assert settings.kulai_whisper_device == "cuda"
            assert settings.kulai_whisper_compute_type == "int8_float16"
            assert settings.kulai_whisper_vad_filter is True

            silence_one = tmp_path / "desktop-silence-one.wav"
            silence_two = tmp_path / "desktop-silence-two.wav"
            _write_silence(silence_one)
            _write_silence(silence_two)
            queued_paths = [silence_one, silence_two]
            explicit_audio = os.environ.get("KULAI_WHISPER_AUDIO")
            if explicit_audio:
                speech_copy = tmp_path / "desktop-explicit-speech.wav"
                shutil.copyfile(explicit_audio, speech_copy)
                queued_paths.append(speech_copy)
            recorder = _QueuedRecorder(queued_paths)

            provider_calls = 0

            def provider_factory(active_settings: Settings):
                nonlocal provider_calls, provider
                from kulai_provider_whisper import WhisperTranscriptionProvider

                from kulai_memory.whisper_provider import (
                    create_whisper_transcription_provider,
                )

                provider_calls += 1
                created = create_whisper_transcription_provider(
                    settings=active_settings
                )
                assert isinstance(created, WhisperTranscriptionProvider)
                provider = created
                return created

            owned_config = database_config_for_database(
                owned.name,
                config=main_config,
            )
            controller = DesktopController(
                settings=settings,
                recorder=recorder,
                provider_factory=provider_factory,
                database_config_factory=lambda: owned_config,
            )
            await controller.startup()

            await controller.start_recording(device_id=0)
            first = await controller.stop_and_process()
            assert first.status is DesktopResultStatus.SKIPPED_EMPTY
            assert first.transcript.strip() == ""
            assert provider is not None
            loaded_model = getattr(provider, "_model", None)
            assert loaded_model is not None

            await controller.start_recording(device_id=0)
            second = await controller.stop_and_process()
            assert second.status is DesktopResultStatus.SKIPPED_EMPTY
            assert second.transcript.strip() == ""
            assert getattr(provider, "_model", None) is loaded_model
            assert provider_calls == 1
            print(
                "desktop_silence statuses=skipped_empty,skipped_empty "
                "model=large-v3 device=cuda compute_type=int8_float16 "
                "vad_filter=true model_reused=true"
            )

            expected_count = 0
            if explicit_audio:
                await controller.start_recording(device_id=0)
                speech = await controller.stop_and_process()
                assert speech.status is DesktopResultStatus.CREATED
                assert speech.transcript.strip()
                assert speech.memory_id is not None
                expected_count = 1
                print(
                    "desktop_explicit_speech status=created "
                    f"char_count={len(speech.transcript)}"
                )
            else:
                print("desktop_explicit_speech status=not_configured")

            verification_engine = create_async_engine(owned_url)
            try:
                async with verification_engine.connect() as connection:
                    count = await connection.scalar(text("SELECT count(*) FROM memories"))
                    vectors = await connection.scalar(
                        text("SELECT count(*) FROM kulai_vector_records")
                    )
                    await connection.rollback()
                assert count == expected_count
                assert vectors == 0
            finally:
                await verification_engine.dispose()
            assert len(recorder.cleaned) == 2 + expected_count
        finally:
            if controller is not None:
                await controller.shutdown()
            await drop_owned_temporary_database(owned, config=main_config)

    asyncio.run(scenario())
