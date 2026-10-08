"""In-process orchestration for the desktop adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from kulai_db import DbConfig
from kulai_transcription import (
    AudioPathInput,
    TranscriptionMode,
    TranscriptionProvider,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionTimestampMode,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kulai_memory.application import (
    MemoryIngestionRetiredError,
    MemoryService,
    MemoryRepository,
    TranscriptFinalEvent,
    TranscriptMemoryIngestionService,
    TranscriptMemoryIngestionStatus,
    TranscriptionService,
    VoiceSession,
    VoiceSessionEvent,
)
from kulai_memory.database_safety import (
    DoctorReport,
    database_config,
    run_database_doctor,
)
from kulai_memory.application.indexing import IndexReconciliationReport
from kulai_memory.automatic_indexing import (
    AutomaticMemoryIndexer, RuntimeMemoryIndexer, RuntimeIndexerFactory,
)
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.runtime import CudaRuntimeConfigurationError, ProcessCudaDllScope
from kulai_memory.settings import Settings, get_settings

from .models import (
    DesktopConfigurationError,
    DesktopDatabaseError,
    DesktopProcessingResult,
    DesktopProgress,
    DesktopProgressState,
    DesktopPublicError,
    DesktopResultStatus,
    DesktopStartupResult,
    DesktopStateError,
    DesktopTranscriptionError,
    MemorySummary,
)
from .recorder import MicrophoneRecorder


RECENT_MEMORY_LIMIT = 20


class _Recorder(Protocol):
    def list_devices(self) -> tuple[Any, ...]: ...
    def start(self, *, device_id: int) -> None: ...
    def stop(self) -> Any: ...
    def cleanup(self, artifact: Any) -> None: ...
    def shutdown(self) -> None: ...


class _SessionFactory(Protocol):
    def __call__(self) -> Any: ...


class _Engine(Protocol):
    async def dispose(self) -> None: ...


ProviderFactory = Callable[[Settings], TranscriptionProvider]
Doctor = Callable[[str], Awaitable[DoctorReport]]
EngineFactory = Callable[[DbConfig], _Engine]
SessionFactoryBuilder = Callable[[_Engine], _SessionFactory]
RepositoryFactory = Callable[[AsyncSession], MemoryRepository]
ProgressCallback = Callable[[DesktopProgress], None]


@dataclass(frozen=True, slots=True)
class _PendingSave:
    transcription: TranscriptionResult
    ingestion_id: UUID
    session_id: UUID


class _DesktopEventSink:
    def __init__(self, callback: ProgressCallback) -> None:
        self._callback = callback

    async def emit(self, event: VoiceSessionEvent) -> None:
        if isinstance(event, TranscriptFinalEvent):
            self._callback(
                DesktopProgress(
                    state=DesktopProgressState.TRANSCRIPT_READY,
                    transcript=event.payload.text,
                )
            )


def _provider_factory(settings: Settings) -> TranscriptionProvider:
    from kulai_memory.whisper_provider import create_whisper_transcription_provider

    return create_whisper_transcription_provider(settings=settings)


async def _doctor(async_url: str) -> DoctorReport:
    return await run_database_doctor(async_url=async_url)


def _engine_factory(config: DbConfig) -> AsyncEngine:
    return create_async_engine(
        config.async_url,
        echo=config.echo,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_pre_ping=True,
    )


def _session_factory_builder(engine: _Engine) -> _SessionFactory:
    return async_sessionmaker(cast(AsyncEngine, engine), expire_on_commit=False)


def _repository_factory(session: AsyncSession) -> MemoryRepository:
    return PostgresMemoryRepository(db=session)


def _noop_progress(progress: DesktopProgress) -> None:
    del progress


class DesktopController:
    """Own one desktop runtime, provider, event loop, and DB connection pool."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        recorder: _Recorder | None = None,
        provider_factory: ProviderFactory = _provider_factory,
        database_config_factory: Callable[[], DbConfig] = database_config,
        doctor: Doctor = _doctor,
        engine_factory: EngineFactory = _engine_factory,
        session_factory_builder: SessionFactoryBuilder = _session_factory_builder,
        repository_factory: RepositoryFactory = _repository_factory,
        progress_callback: ProgressCallback = _noop_progress,
        indexer_factory: RuntimeIndexerFactory = AutomaticMemoryIndexer,
    ) -> None:
        self._settings = settings or get_settings()
        self._recorder = recorder or MicrophoneRecorder()
        self._provider_factory = provider_factory
        self._database_config_factory = database_config_factory
        self._doctor = doctor
        self._engine_factory = engine_factory
        self._session_factory_builder = session_factory_builder
        self._repository_factory = repository_factory
        self._progress_callback = progress_callback
        self._indexer_factory = indexer_factory
        self._indexer: RuntimeMemoryIndexer | None = None
        self._indexing_report = IndexReconciliationReport()
        self._operation_lock = asyncio.Lock()
        self._cuda_scope = ProcessCudaDllScope(self._settings.kulai_cuda_dll_dir)
        self._engine: _Engine | None = None
        self._session_factory: _SessionFactory | None = None
        self._provider: TranscriptionProvider | None = None
        self._transcription_service: TranscriptionService | None = None
        self._active_ingestion_id: UUID | None = None
        self._pending: _PendingSave | None = None
        self._started = False
        self._closed = False

    @property
    def has_pending_save(self) -> bool:
        return self._pending is not None

    @property
    def provider(self) -> TranscriptionProvider | None:
        """Expose the owned provider for safe diagnostics and reuse tests."""

        return self._provider

    async def startup(self) -> DesktopStartupResult:
        async with self._operation_lock:
            if self._closed:
                raise DesktopStateError
            if self._started:
                return DesktopStartupResult(
                    devices=await asyncio.to_thread(self._recorder.list_devices),
                    memories=await self._list_recent_unlocked(RECENT_MEMORY_LIMIT),
                    indexing=self._indexing_report,
                )

            self._validate_canonical_stt()
            try:
                config = self._database_config_factory()
                report = await self._doctor(config.async_url)
            except asyncio.CancelledError:
                raise
            except DesktopPublicError:
                raise
            except Exception as exc:
                raise DesktopDatabaseError from exc
            if not report.ok:
                raise DesktopDatabaseError

            unexpected_error: type[DesktopPublicError] = DesktopPublicError
            try:
                self._cuda_scope.activate()
            except CudaRuntimeConfigurationError as exc:
                await self._shutdown_resources_unlocked()
                raise DesktopConfigurationError from exc
            try:
                unexpected_error = DesktopDatabaseError
                engine = self._engine_factory(config)
                self._engine = engine
                session_factory = self._session_factory_builder(engine)
                self._session_factory = session_factory
                unexpected_error = DesktopPublicError
                provider = self._provider_factory(self._settings)
                self._indexer = self._indexer_factory(
                    settings=self._settings, session_factory=session_factory,
                )
                devices = await asyncio.to_thread(self._recorder.list_devices)
                self._provider = provider
                self._transcription_service = TranscriptionService(provider=provider)
                self._indexing_report = await self._indexer.reconcile()
                self._started = True
                memories = await self._list_recent_unlocked(RECENT_MEMORY_LIMIT)
                return DesktopStartupResult(devices=devices, memories=memories, indexing=self._indexing_report)
            except asyncio.CancelledError:
                await self._shutdown_resources_unlocked()
                raise
            except DesktopPublicError:
                await self._shutdown_resources_unlocked()
                raise
            except Exception as exc:
                await self._shutdown_resources_unlocked()
                raise unexpected_error() from exc

    async def start_recording(self, *, device_id: int) -> UUID:
        async with self._operation_lock:
            self._require_started()
            if self._pending is not None or self._active_ingestion_id is not None:
                raise DesktopStateError
            ingestion_id = uuid4()
            await asyncio.to_thread(self._recorder.start, device_id=device_id)
            self._active_ingestion_id = ingestion_id
            return ingestion_id

    async def stop_and_process(self) -> DesktopProcessingResult:
        async with self._operation_lock:
            self._require_started()
            ingestion_id = self._active_ingestion_id
            if ingestion_id is None:
                raise DesktopStateError

            artifact = None
            self._active_ingestion_id = None
            try:
                artifact = await asyncio.to_thread(self._recorder.stop)
                self._progress_callback(
                    DesktopProgress(state=DesktopProgressState.TRANSCRIBING)
                )
                transcription, session_id = await self._transcribe_unlocked(artifact.path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, DesktopStateError):
                    raise
                raise DesktopTranscriptionError from exc
            finally:
                if artifact is not None:
                    try:
                        await asyncio.to_thread(self._recorder.cleanup, artifact)
                    except Exception:
                        pass

            self._pending = _PendingSave(
                transcription=transcription,
                ingestion_id=ingestion_id,
                session_id=session_id,
            )
            return await self._save_pending_unlocked()

    async def retry_save(self) -> DesktopProcessingResult:
        async with self._operation_lock:
            self._require_started()
            if self._pending is None:
                raise DesktopStateError
            return await self._save_pending_unlocked()

    async def list_recent(
        self, *, limit: int = RECENT_MEMORY_LIMIT
    ) -> tuple[MemorySummary, ...]:
        async with self._operation_lock:
            self._require_started()
            return await self._list_recent_unlocked(limit)

    async def shutdown(self) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._active_ingestion_id = None
            self._pending = None
            await self._shutdown_resources_unlocked()

    async def reconcile_missing_indexes(self) -> IndexReconciliationReport:
        async with self._operation_lock:
            self._require_started()
            if self._indexer is None:
                raise DesktopStateError
            self._indexing_report = await self._indexer.reconcile()
            return self._indexing_report

    def _validate_canonical_stt(self) -> None:
        if (
            self._settings.kulai_whisper_model != "large-v3"
            or self._settings.kulai_whisper_device != "cuda"
            or self._settings.kulai_whisper_compute_type != "int8_float16"
            or self._settings.kulai_whisper_vad_filter is not True
            or self._settings.kulai_embedding_model != "bge-m3:567m-fp16"
            or self._settings.kulai_vector_dimension != 1024
        ):
            raise DesktopConfigurationError

    def _require_started(self) -> None:
        if not self._started or self._closed:
            raise DesktopStateError

    async def _transcribe_unlocked(
        self, audio_path: Path
    ) -> tuple[TranscriptionResult, UUID]:
        service = self._transcription_service
        if service is None:
            raise DesktopStateError
        session = VoiceSession(
            event_sink=_DesktopEventSink(self._progress_callback),
            transcription_service=service,
        )
        try:
            await session.start()
            result = await session.transcribe(
                request=TranscriptionRequest(
                    audio=AudioPathInput(path=audio_path),
                    mode=TranscriptionMode.TRANSCRIBE,
                    timestamp_mode=TranscriptionTimestampMode.NONE,
                )
            )
            return result, session.session_id
        finally:
            await session.close()

    async def _save_pending_unlocked(self) -> DesktopProcessingResult:
        pending = self._pending
        factory = self._session_factory
        if pending is None or factory is None:
            raise DesktopStateError
        self._progress_callback(DesktopProgress(state=DesktopProgressState.SAVING))

        try:
            async with factory() as session:
                service = TranscriptMemoryIngestionService(
                    memory_service=MemoryService(
                        repository=self._repository_factory(
                            cast(AsyncSession, session)
                        )
                    )
                )
                result = await service.ingest(
                    transcription=pending.transcription,
                    ingestion_id=pending.ingestion_id,
                    session_id=pending.session_id,
                )
                if result.status is not TranscriptMemoryIngestionStatus.SKIPPED_EMPTY:
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except MemoryIngestionRetiredError:
            self._pending = None
            return DesktopProcessingResult(
                status=DesktopResultStatus.INGESTION_RETIRED,
                transcript=pending.transcription.text,
                memory_id=None,
                save_pending=False,
            )
        except Exception:
            return DesktopProcessingResult(
                status=DesktopResultStatus.SAVE_FAILED,
                transcript=pending.transcription.text,
                memory_id=None,
                save_pending=True,
            )

        status = DesktopResultStatus(result.status.value)
        memory_id = result.memory.id if result.memory is not None else None
        if result.memory is not None:
            self._progress_callback(DesktopProgress(state=DesktopProgressState.INDEXING))
            try:
                if self._indexer is None:
                    raise DesktopStateError
                await self._indexer.ensure(memory=result.memory)
            except asyncio.CancelledError:
                raise
            except Exception:
                return DesktopProcessingResult(
                    status=DesktopResultStatus.INDEXING_FAILED,
                    transcript=pending.transcription.text,
                    memory_id=memory_id,
                    save_pending=True,
                )
        self._pending = None
        return DesktopProcessingResult(
            status=status,
            transcript=pending.transcription.text,
            memory_id=memory_id,
            save_pending=False,
        )

    async def _list_recent_unlocked(self, limit: int) -> tuple[MemorySummary, ...]:
        factory = self._session_factory
        if factory is None:
            raise DesktopStateError
        try:
            async with factory() as session:
                memories = await MemoryService(
                    repository=self._repository_factory(cast(AsyncSession, session))
                ).list_recent_memories(limit=limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DesktopDatabaseError from exc
        return tuple(
            MemorySummary(
                id=memory.id,
                created_at=memory.created_at,
                content=memory.content,
            )
            for memory in memories
        )

    async def _shutdown_resources_unlocked(self) -> None:
        indexer = self._indexer
        self._indexer = None
        cancellation = None
        if indexer is not None:
            try:
                await indexer.aclose()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                pass
        try:
            await asyncio.to_thread(self._recorder.shutdown)
        except Exception:
            pass
        engine = self._engine
        self._engine = None
        self._session_factory = None
        self._transcription_service = None
        self._provider = None
        self._started = False
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                pass
        self._cuda_scope.close()
        if cancellation is not None:
            raise cancellation
