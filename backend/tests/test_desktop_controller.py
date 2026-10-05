from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from kulai_db import DbConfig
from kulai_transcription import (
    AudioInputKind,
    TranscriptionCapabilities,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
)

from kulai_memory.application import IdempotentMemoryWrite, Memory
from kulai_memory.database_safety import CheckResult, DoctorReport
from kulai_memory.desktop.controller import DesktopController
from kulai_memory.desktop.models import (
    DesktopConfigurationError,
    DesktopDatabaseError,
    DesktopDependencyError,
    DesktopProgressState,
    DesktopPublicError,
    DesktopRecordingError,
    DesktopResultStatus,
    MicrophoneDevice,
    RecordingArtifact,
)
from kulai_memory.settings import Settings


class FakeProvider:
    provider_id = "desktop-fake"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.PATH}),
        modes=frozenset({TranscriptionMode.TRANSCRIBE}),
        supports_language_hint=True,
        supports_language_detection=True,
        supports_segments=True,
        supports_segment_timestamps=False,
    )

    def __init__(self, texts: Sequence[str]) -> None:
        self._texts = tuple(texts)
        self.requests: list[TranscriptionRequest] = []

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._texts) - 1)
        return TranscriptionResult(
            text=self._texts[index],
            provider_id=self.provider_id,
            model_id="large-v3",
            mode=request.mode,
            segments=(),
            duration_seconds=0.25,
        )


class FakeRecorder:
    def __init__(self, root: Path, *, list_error: Exception | None = None) -> None:
        self.root = root
        self.list_error = list_error
        self.recording = False
        self.index = 0
        self.started_devices: list[int] = []
        self.cleaned: list[Path] = []
        self.shutdown_count = 0

    def list_devices(self) -> tuple[MicrophoneDevice, ...]:
        if self.list_error is not None:
            raise self.list_error
        return (MicrophoneDevice(3, "Fake mic", "Test", True),)

    def start(self, *, device_id: int) -> None:
        assert not self.recording
        self.recording = True
        self.started_devices.append(device_id)

    def stop(self) -> RecordingArtifact:
        assert self.recording
        self.recording = False
        path = self.root / f"recording-{self.index}.wav"
        self.index += 1
        path.write_bytes(b"RIFF fake")
        return RecordingArtifact(path=path, duration_seconds=0.25, limit_reached=False)

    def cleanup(self, artifact: RecordingArtifact) -> None:
        artifact.path.unlink(missing_ok=True)
        self.cleaned.append(artifact.path)

    def shutdown(self) -> None:
        self.shutdown_count += 1


class InMemoryRepository:
    def __init__(self) -> None:
        self.by_ingestion: dict[UUID, Memory] = {}
        self.create_or_get_calls: list[Memory] = []

    async def create(self, memory: Memory) -> Memory:
        self.by_ingestion[memory.ingestion_id] = memory
        return memory

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        self.create_or_get_calls.append(memory)
        existing = self.by_ingestion.get(memory.ingestion_id)
        if existing is not None:
            return IdempotentMemoryWrite(memory=existing, created=False)
        await self.create(memory)
        return IdempotentMemoryWrite(memory=memory, created=True)

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        return next(
            (item for item in self.by_ingestion.values() if item.id == memory_id),
            None,
        )

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        return tuple(
            sorted(
                self.by_ingestion.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )[:limit]
        )


class FakeSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.commits = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("private database detail")


class FakeSessionFactory:
    def __init__(self, commit_failures: Sequence[bool] = ()) -> None:
        self._commit_failures = list(commit_failures)
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        fail = self._commit_failures.pop(0) if self._commit_failures else False
        session = FakeSession(fail_commit=fail)
        self.sessions.append(session)
        return session


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_count = 0

    async def dispose(self) -> None:
        self.dispose_count += 1


class Harness:
    def __init__(
        self,
        root: Path,
        *,
        texts: Sequence[str] = ("Zapamiętaj spotkanie",),
        commit_failures: Sequence[bool] = (),
        doctor_ok: bool = True,
        cuda_directory: Path | None = None,
        recorder_error: Exception | None = None,
        provider_error: Exception | None = None,
    ) -> None:
        self.recorder = FakeRecorder(root, list_error=recorder_error)
        self.provider = FakeProvider(texts)
        self.provider_factory_calls = 0
        self.repository = InMemoryRepository()
        self.engine = FakeEngine()
        self.sessions = FakeSessionFactory(commit_failures)
        self.progress: list[Any] = []
        self.doctor_ok = doctor_ok
        self.cuda_directory = cuda_directory
        self.provider_error = provider_error

    def provider_factory(self, settings: Settings) -> FakeProvider:
        assert settings.kulai_whisper_model == "large-v3"
        if self.cuda_directory is not None:
            assert os.environ["PATH"].split(os.pathsep)[0] == str(
                self.cuda_directory.resolve()
            )
        self.provider_factory_calls += 1
        if self.provider_error is not None:
            raise self.provider_error
        return self.provider

    async def doctor(self, async_url: str) -> DoctorReport:
        assert async_url.startswith("postgresql+asyncpg://")
        return DoctorReport((CheckResult(name="test", ok=self.doctor_ok),))

    def controller(self) -> DesktopController:
        config = DbConfig(user="u", password="p", name="db")
        return DesktopController(
            settings=Settings(
                _env_file=None,
                kulai_cuda_dll_dir=self.cuda_directory,
            ),
            recorder=self.recorder,
            provider_factory=self.provider_factory,
            database_config_factory=lambda: config,
            doctor=self.doctor,
            engine_factory=lambda ignored: self.engine,
            session_factory_builder=lambda ignored: self.sessions,
            repository_factory=lambda ignored: self.repository,
            progress_callback=self.progress.append,
        )


def test_record_transcribe_create_recent_and_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path)
        controller = harness.controller()

        startup = await controller.startup()
        first_id = await controller.start_recording(device_id=3)
        created = await controller.stop_and_process()
        recent = await controller.list_recent()
        await controller.shutdown()
        await controller.shutdown()

        assert startup.devices[0].is_default is True
        assert startup.memories == ()
        assert created.status is DesktopResultStatus.CREATED
        assert created.transcript == "Zapamiętaj spotkanie"
        assert created.memory_id == recent[0].id
        assert harness.repository.create_or_get_calls[0].ingestion_id == first_id
        assert [item.state for item in harness.progress] == [
            DesktopProgressState.TRANSCRIBING,
            DesktopProgressState.TRANSCRIPT_READY,
            DesktopProgressState.SAVING,
        ]
        assert harness.recorder.cleaned == [tmp_path / "recording-0.wav"]
        assert not (tmp_path / "recording-0.wav").exists()
        assert harness.provider_factory_calls == 1
        assert harness.engine.dispose_count == 1
        assert harness.recorder.shutdown_count == 1

    asyncio.run(scenario())


def test_empty_transcript_is_skipped_without_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, texts=("   ",))
        controller = harness.controller()
        await controller.startup()
        await controller.start_recording(device_id=3)

        result = await controller.stop_and_process()

        assert result.status is DesktopResultStatus.SKIPPED_EMPTY
        assert result.memory_id is None
        assert not result.save_pending
        assert harness.repository.create_or_get_calls == []
        await controller.shutdown()

    asyncio.run(scenario())


def test_failed_commit_keeps_pending_and_retry_uses_same_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, commit_failures=(False, True, False))
        controller = harness.controller()
        await controller.startup()
        ingestion_id = await controller.start_recording(device_id=3)

        failed = await controller.stop_and_process()
        assert controller.has_pending_save is True
        retried = await controller.retry_save()

        assert failed.status is DesktopResultStatus.SAVE_FAILED
        assert failed.save_pending is True
        assert controller.has_pending_save is False
        assert retried.status is DesktopResultStatus.DUPLICATE
        assert retried.save_pending is False
        assert len(harness.provider.requests) == 1
        assert len(harness.repository.create_or_get_calls) == 2
        first, second = harness.repository.create_or_get_calls
        assert first.ingestion_id == second.ingestion_id == ingestion_id
        assert first.session_id == second.session_id
        assert retried.memory_id == harness.repository.by_ingestion[ingestion_id].id
        await controller.shutdown()

    asyncio.run(scenario())


def test_new_recordings_get_new_ids_and_reuse_one_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, texts=("pierwsza", "druga"))
        controller = harness.controller()
        await controller.startup()

        first_id = await controller.start_recording(device_id=3)
        first = await controller.stop_and_process()
        second_id = await controller.start_recording(device_id=3)
        second = await controller.stop_and_process()

        assert first.status is DesktopResultStatus.CREATED
        assert second.status is DesktopResultStatus.CREATED
        assert first_id != second_id
        assert harness.provider_factory_calls == 1
        assert controller.provider is harness.provider
        assert len(harness.provider.requests) == 2
        await controller.shutdown()

    asyncio.run(scenario())


def test_database_doctor_failure_blocks_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, doctor_ok=False)
        controller = harness.controller()

        with pytest.raises(DesktopDatabaseError):
            await controller.startup()

        assert harness.provider_factory_calls == 0
        assert harness.recorder.started_devices == []
        await controller.shutdown()

    asyncio.run(scenario())


def test_microphone_startup_error_is_not_reclassified_as_database(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, recorder_error=DesktopRecordingError())
        controller = harness.controller()

        with pytest.raises(DesktopRecordingError):
            await controller.startup()

        assert harness.provider_factory_calls == 1
        assert harness.engine.dispose_count == 1
        await controller.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "public_error_type",
    [DesktopConfigurationError, DesktopDependencyError],
)
def test_existing_public_startup_errors_keep_their_type(
    tmp_path: Path,
    public_error_type: type[DesktopPublicError],
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, provider_error=public_error_type())
        controller = harness.controller()

        with pytest.raises(public_error_type):
            await controller.startup()

        assert harness.engine.dispose_count == 1
        await controller.shutdown()

    asyncio.run(scenario())


def test_unexpected_provider_startup_error_becomes_generic_public_error(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        internal = RuntimeError("private provider detail")
        harness = Harness(tmp_path, provider_error=internal)
        controller = harness.controller()

        with pytest.raises(DesktopPublicError) as caught:
            await controller.startup()

        assert type(caught.value) is DesktopPublicError
        assert caught.value.public_message == DesktopPublicError.safe_message
        assert caught.value.__cause__ is internal
        assert "private" not in str(caught.value)
        assert harness.engine.dispose_count == 1
        await controller.shutdown()

    asyncio.run(scenario())


def test_cuda_directory_changes_only_process_path_for_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        cuda_directory = tmp_path / "cuda"
        cuda_directory.mkdir()
        original_path = os.environ.get("PATH", "")
        harness = Harness(tmp_path, cuda_directory=cuda_directory)
        controller = harness.controller()

        await controller.startup()
        assert os.environ["PATH"].split(os.pathsep)[0] == str(
            cuda_directory.resolve()
        )
        await controller.shutdown()

        assert os.environ.get("PATH", "") == original_path

    asyncio.run(scenario())
