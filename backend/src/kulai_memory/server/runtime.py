"""Long-lived process runtime used by the WebSocket adapter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

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
    EventSink,
    MemoryIdempotencyConflictError,
    MemoryIngestionRetiredError,
    MemoryArchivedError,
    MemoryRepository,
    MemoryService,
    TranscriptMemoryIngestionResult,
    TranscriptMemoryIngestionService,
    TranscriptMemoryIngestionStatus,
    TranscriptionService,
    VoiceSession,
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

logger = logging.getLogger(__name__)


class _SessionFactory(Protocol):
    def __call__(self) -> Any: ...


class _Engine(Protocol):
    async def dispose(self) -> None: ...


ProviderFactory = Callable[[Settings], TranscriptionProvider]
Doctor = Callable[[str], Awaitable[DoctorReport]]
EngineFactory = Callable[[DbConfig], _Engine]
SessionFactoryBuilder = Callable[[_Engine], _SessionFactory]
RepositoryFactory = Callable[[AsyncSession], MemoryRepository]


class ServerRuntimeError(RuntimeError):
    """Safe base error for the voice-memory server runtime."""

    code = "server.unavailable"
    safe_message = "The voice-memory service is unavailable."

    def __init__(self) -> None:
        self.public_message = self.safe_message
        super().__init__(self.public_message)


class ServerConfigurationError(ServerRuntimeError):
    code = "server.configuration_invalid"
    safe_message = "The voice-memory server configuration is invalid."


class ServerDatabaseError(ServerRuntimeError):
    code = "server.database_unavailable"
    safe_message = "The voice-memory database is unavailable."


class ServerPersistenceError(ServerRuntimeError):
    code = "memory.save_failed"
    safe_message = "The memory could not be saved. Retry is available."


class ServerIndexingError(ServerRuntimeError):
    code = "memory.index_failed"
    safe_message = "The memory was saved, but semantic indexing could not be completed. Retry is available."

    def __init__(self, *, memory_id: UUID) -> None:
        self.memory_id = memory_id
        super().__init__()


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


async def _wait_for_uncancellable_operation(task: asyncio.Task[Any]) -> None:
    """Wait until a thread-backed provider has stopped using its input path."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if task.done() and not task.cancelled():
        try:
            task.result()
        except Exception:
            pass


class VoiceMemoryServerRuntime:
    """Own one provider, DB pool and serialized inference context per process."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        provider_factory: ProviderFactory = _provider_factory,
        database_config_factory: Callable[[], DbConfig] = database_config,
        doctor: Doctor = _doctor,
        engine_factory: EngineFactory = _engine_factory,
        session_factory_builder: SessionFactoryBuilder = _session_factory_builder,
        repository_factory: RepositoryFactory = _repository_factory,
        indexer_factory: RuntimeIndexerFactory = AutomaticMemoryIndexer,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider_factory = provider_factory
        self._database_config_factory = database_config_factory
        self._doctor = doctor
        self._engine_factory = engine_factory
        self._session_factory_builder = session_factory_builder
        self._repository_factory = repository_factory
        self._indexer_factory = indexer_factory
        self._indexer: RuntimeMemoryIndexer | None = None
        self._indexing_report = IndexReconciliationReport()
        self._cuda_scope = ProcessCudaDllScope(self._settings.kulai_cuda_dll_dir)
        self._lifecycle_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()
        self._inference_tasks: set[asyncio.Task[Any]] = set()
        self._engine: _Engine | None = None
        self._session_factory: _SessionFactory | None = None
        self._provider: TranscriptionProvider | None = None
        self._transcription_service: TranscriptionService | None = None
        self._started = False
        self._closed = False

    @property
    def provider(self) -> TranscriptionProvider | None:
        return self._provider

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    @property
    def indexing_report(self) -> IndexReconciliationReport:
        return self._indexing_report

    async def startup(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise ServerRuntimeError
            if self._started:
                return
            self._validate_canonical_stt()
            try:
                config = self._database_config_factory()
                report = await self._doctor(config.async_url)
            except asyncio.CancelledError:
                raise
            except ServerRuntimeError:
                raise
            except Exception as exc:
                raise ServerDatabaseError from exc
            if not report.ok:
                raise ServerDatabaseError

            try:
                self._cuda_scope.activate()
                self._engine = self._engine_factory(config)
                self._session_factory = self._session_factory_builder(self._engine)
                self._provider = self._provider_factory(self._settings)
                self._transcription_service = TranscriptionService(
                    provider=self._provider
                )
                self._indexer = self._indexer_factory(
                    settings=self._settings, session_factory=self._session_factory,
                )
                self._indexing_report = await self._indexer.reconcile()
                self._report_indexing_state()
                self._started = True
            except asyncio.CancelledError:
                await self._shutdown_resources()
                raise
            except CudaRuntimeConfigurationError as exc:
                await self._shutdown_resources()
                raise ServerConfigurationError from exc
            except Exception as exc:
                await self._shutdown_resources()
                raise ServerRuntimeError from exc

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            tasks = tuple(self._inference_tasks)
            if tasks:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in tasks),
                    return_exceptions=True,
                )
            await self._shutdown_resources()

    def create_voice_session(
        self, *, event_sink: EventSink, session_id: UUID
    ) -> VoiceSession:
        self._require_started()
        service = self._transcription_service
        if service is None:
            raise ServerRuntimeError
        return VoiceSession(
            event_sink=event_sink,
            transcription_service=service,
            session_id=session_id,
        )

    async def transcribe(
        self, *, session: VoiceSession, audio_path: Path
    ) -> TranscriptionResult:
        self._require_started()
        async with self._inference_lock:
            self._require_started()
            operation = asyncio.create_task(
                session.transcribe(
                    request=TranscriptionRequest(
                        audio=AudioPathInput(path=audio_path),
                        mode=TranscriptionMode.TRANSCRIBE,
                        timestamp_mode=TranscriptionTimestampMode.NONE,
                    )
                )
            )
            self._inference_tasks.add(operation)
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                await _wait_for_uncancellable_operation(operation)
                raise
            finally:
                self._inference_tasks.discard(operation)

    async def ingest(
        self,
        *,
        transcription: TranscriptionResult,
        ingestion_id: UUID,
        session_id: UUID,
    ) -> TranscriptMemoryIngestionResult:
        self._require_started()
        factory = self._session_factory
        if factory is None:
            raise ServerRuntimeError
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
                    transcription=transcription,
                    ingestion_id=ingestion_id,
                    session_id=session_id,
                )
                if result.status is not TranscriptMemoryIngestionStatus.SKIPPED_EMPTY:
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except (MemoryIdempotencyConflictError, MemoryIngestionRetiredError, MemoryArchivedError):
            raise
        except Exception as exc:
            raise ServerPersistenceError from exc

        # Canonical COMMIT and session close precede any embedding request.
        if result.memory is not None:
            try:
                if self._indexer is None:
                    raise ServerRuntimeError
                await self._indexer.ensure(memory=result.memory)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ServerIndexingError(memory_id=result.memory.id) from None
        return result

    async def reconcile_missing_indexes(self) -> IndexReconciliationReport:
        self._require_started()
        if self._indexer is None:
            raise ServerRuntimeError
        self._indexing_report = await self._indexer.reconcile()
        self._report_indexing_state()
        return self._indexing_report

    def _report_indexing_state(self) -> None:
        report = self._indexing_report
        if report.degraded:
            logger.warning(
                "Semantic indexing degraded: missing=%d incompatible=%d failed=%d code=%s",
                report.remaining_missing, report.incompatible_existing, report.failed, report.error_code,
            )

    def _validate_canonical_stt(self) -> None:
        if (
            self._settings.kulai_whisper_model != "large-v3"
            or self._settings.kulai_whisper_device != "cuda"
            or self._settings.kulai_whisper_compute_type != "int8_float16"
            or self._settings.kulai_whisper_vad_filter is not True
            or self._settings.kulai_embedding_model != "bge-m3:567m-fp16"
            or self._settings.kulai_vector_dimension != 1024
        ):
            raise ServerConfigurationError

    def _require_started(self) -> None:
        if not self._started or self._closed:
            raise ServerRuntimeError

    async def _shutdown_resources(self) -> None:
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
        engine = self._engine
        self._engine = None
        self._session_factory = None
        self._provider = None
        self._transcription_service = None
        self._started = False
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                pass
        self._cuda_scope.close()
        if cancellation is not None:
            raise cancellation
