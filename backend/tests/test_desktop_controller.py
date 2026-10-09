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

from kulai_memory.application import IdempotentMemoryWrite, Memory, MemoryIngestionRetiredError
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
    DesktopStateError,
    MicrophoneDevice,
    RecordingArtifact,
)
from kulai_memory.settings import Settings
from backend.tests.indexing_fakes import FakeRuntimeIndexer
from backend.tests.desktop_rag_fakes import FakeDesktopRag, FakeOwnedBGE


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
        self.closed = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True
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
        self.indexer = FakeRuntimeIndexer()
        self.embedding = FakeOwnedBGE()
        self.rag = FakeDesktopRag()
        self.embedding_factory_calls = self.rag_factory_calls = 0

    def embedding_factory(self, **kwargs):
        self.embedding_factory_calls += 1
        return self.embedding

    def rag_factory(self, **kwargs):
        self.rag_factory_calls += 1
        self.rag.provider = kwargs["embedding_provider"]
        return self.rag

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
                kulai_vector_dimension=1024,
            ),
            recorder=self.recorder,
            provider_factory=self.provider_factory,
            database_config_factory=lambda: config,
            doctor=self.doctor,
            engine_factory=lambda ignored: self.engine,
            session_factory_builder=lambda ignored: self.sessions,
            repository_factory=lambda ignored: self.repository,
            progress_callback=self.progress.append,
            indexer_factory=lambda **kwargs: self.indexer,
            embedding_provider_factory=self.embedding_factory,
            rag_runtime_factory=self.rag_factory,
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
            DesktopProgressState.INDEXING,
        ]
        assert harness.recorder.cleaned == [tmp_path / "recording-0.wav"]
        assert not (tmp_path / "recording-0.wav").exists()
        assert harness.provider_factory_calls == 1
        assert harness.engine.dispose_count == 1
        assert harness.recorder.shutdown_count == 1
        assert harness.indexer.calls[0].id == created.memory_id
        assert harness.indexer.closed == 1
        assert harness.indexer.reconciliations == 1

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
        assert harness.indexer.calls == []
        await controller.shutdown()

    asyncio.run(scenario())


def test_indexing_failure_preserves_id_commit_and_pending_then_repairs_without_stt(tmp_path):
    async def scenario():
        harness = Harness(tmp_path)
        controller = harness.controller()
        await controller.startup()
        ingestion_id = await controller.start_recording(device_id=3)
        original_ensure = harness.indexer.ensure
        async def ensure_after_commit(*, memory):
            assert all(session.closed for session in harness.sessions.sessions)
            assert harness.sessions.sessions[-1].commits == 1
            return await original_ensure(memory=memory)
        harness.indexer.ensure = ensure_after_commit
        harness.indexer.error = RuntimeError("PRIVATE_INDEX_ERROR")
        result = await controller.stop_and_process()
        assert result.status is DesktopResultStatus.INDEXING_FAILED
        assert result.memory_id is not None and result.save_pending
        assert harness.repository.by_ingestion[ingestion_id].id == result.memory_id
        assert (await controller.list_recent())[0].id == result.memory_id
        harness.indexer.error = None
        retry = await controller.retry_save()
        assert retry.status is DesktopResultStatus.DUPLICATE and not retry.save_pending
        assert retry.memory_id == result.memory_id
        assert len(harness.repository.by_ingestion) == len(harness.provider.requests) == 1
        assert len(harness.indexer.calls) == 2
        await controller.shutdown()
        assert harness.indexer.closed == 1
    asyncio.run(scenario())


def test_startup_degraded_report_and_later_reconciliation(tmp_path):
    from kulai_memory.application.indexing import IndexReconciliationReport
    async def scenario():
        harness = Harness(tmp_path)
        harness.indexer.report = IndexReconciliationReport(failed=1, remaining_missing=2)
        controller = harness.controller()
        assert (await controller.startup()).indexing.degraded
        harness.indexer.report = IndexReconciliationReport(indexed=2)
        assert not (await controller.reconcile_missing_indexes()).degraded
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


@pytest.mark.parametrize("after_save_failure", [False, True])
def test_retired_ingestion_finishes_pending_flow_without_retry_loop(tmp_path, after_save_failure):
    async def scenario():
        harness = Harness(tmp_path, texts=("synthetic note", "fresh note"),
                          commit_failures=(False, True) if after_save_failure else ())
        controller = harness.controller()
        await controller.startup()
        ingestion_id = await controller.start_recording(device_id=3)
        original_create = harness.repository.create_or_get_by_ingestion_id

        async def retired(memory):
            assert memory.ingestion_id == ingestion_id
            raise MemoryIngestionRetiredError()

        if after_save_failure:
            failed = await controller.stop_and_process()
            assert failed.save_pending and failed.status is DesktopResultStatus.SAVE_FAILED
            harness.repository.create_or_get_by_ingestion_id = retired
            result = await controller.retry_save()
        else:
            harness.repository.create_or_get_by_ingestion_id = retired
            result = await controller.stop_and_process()
        assert result.status is DesktopResultStatus.INGESTION_RETIRED
        assert not result.save_pending and not controller.has_pending_save
        assert result.memory_id is None
        assert len(harness.provider.requests) == 1
        with pytest.raises(DesktopStateError):
            await controller.retry_save()
        harness.repository.create_or_get_by_ingestion_id = original_create
        assert await controller.start_recording(device_id=3) != ingestion_id
        assert (await controller.stop_and_process()).status is DesktopResultStatus.CREATED
        await controller.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("error,status", [
    ("archived", DesktopResultStatus.INGESTION_ARCHIVED),
    ("conflict", DesktopResultStatus.INGESTION_CONFLICT),
])
def test_archived_or_edited_ingestion_is_terminal_without_retranscription(tmp_path, error, status):
    from kulai_memory.application import MemoryArchivedError, MemoryIdempotencyConflictError
    async def scenario():
        harness = Harness(tmp_path, texts=("synthetic note",))
        controller = harness.controller()
        await controller.startup()
        await controller.start_recording(device_id=3)
        async def reject(memory):
            raise MemoryArchivedError() if error == "archived" else MemoryIdempotencyConflictError()
        harness.repository.create_or_get_by_ingestion_id = reject
        result = await controller.stop_and_process()
        assert result.status is status and not result.save_pending and not controller.has_pending_save
        assert len(harness.provider.requests) == 1
        with pytest.raises(DesktopStateError): await controller.retry_save()
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
        assert harness.indexer.closed == 1
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
