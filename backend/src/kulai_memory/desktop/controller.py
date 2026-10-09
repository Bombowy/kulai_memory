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
    MemoryArchivedError,
    MemoryIdempotencyConflictError,
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
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER, MemoryRagError, MemoryRagResult
from kulai_memory.application.retrieval import MemoryRetrievalError, MemoryRetrievalService
from kulai_memory.automatic_indexing import (
    AutomaticMemoryIndexer, RuntimeMemoryIndexer, RuntimeIndexerFactory,
    EmbeddingProviderFactory, OwnedEmbeddingProvider,
)
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.rag_runtime import MemoryRagRuntime
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
    DesktopRagCitation,
    DesktopRagConfigurationError,
    DesktopRagInputError,
    DesktopRagProgress,
    DesktopRagProgressState,
    DesktopRagResult,
    DesktopRagStatus,
    DesktopRecordingError,
    DesktopVoiceMode,
    DesktopVoiceQuestionProgress,
    DesktopVoiceQuestionProgressState,
    DesktopVoiceQuestionResult,
    DesktopVoiceQuestionTranscriptionError,
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
ProgressCallback = Callable[[DesktopProgress | DesktopRagProgress | DesktopVoiceQuestionProgress], None]


class DesktopMemoryRag(Protocol):
    async def __aenter__(self) -> DesktopMemoryRag: ...
    async def ask(self, *, query: str, top_k: int = 5,
                  on_generating: Callable[[], None] | None = None) -> MemoryRagResult: ...
    async def aclose(self) -> None: ...


RagRuntimeFactory = Callable[..., DesktopMemoryRag]


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


def _noop_progress(progress: DesktopProgress | DesktopRagProgress | DesktopVoiceQuestionProgress) -> None:
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
        embedding_provider_factory: EmbeddingProviderFactory = create_embedding_provider,
        rag_runtime_factory: RagRuntimeFactory = MemoryRagRuntime,
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
        self._embedding_provider_factory = embedding_provider_factory
        self._rag_runtime_factory = rag_runtime_factory
        self._embedding_provider: OwnedEmbeddingProvider | None = None
        self._rag: DesktopMemoryRag | None = None
        self._rag_task: asyncio.Task[Any] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closing = False
        self._indexer: RuntimeMemoryIndexer | None = None
        self._indexing_report = IndexReconciliationReport()
        self._operation_lock = asyncio.Lock()
        self._cuda_scope = ProcessCudaDllScope(self._settings.kulai_cuda_dll_dir)
        self._engine: _Engine | None = None
        self._session_factory: _SessionFactory | None = None
        self._provider: TranscriptionProvider | None = None
        self._transcription_service: TranscriptionService | None = None
        self._active_ingestion_id: UUID | None = None
        self._voice_mode: DesktopVoiceMode | None = None
        self._voice_question_task: asyncio.Task[Any] | None = None
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
                self._provider = provider
                unexpected_error = DesktopRagConfigurationError
                self._embedding_provider = self._embedding_provider_factory(settings=self._settings)
                self._indexer = self._indexer_factory(
                    settings=self._settings, session_factory=session_factory,
                    provider=self._embedding_provider,
                )
                self._rag = self._rag_runtime_factory(
                    settings=self._settings, session_factory=session_factory,
                    embedding_provider=self._embedding_provider,
                )
                await self._rag.__aenter__()
                unexpected_error = DesktopPublicError
                devices = await asyncio.to_thread(self._recorder.list_devices)
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
        if self._operation_lock.locked():
            raise DesktopStateError
        async with self._operation_lock:
            self._require_started()
            if (self._pending is not None or self._voice_mode is not None
                    or self._voice_question_task is not None):
                raise DesktopStateError
            ingestion_id = uuid4()
            await asyncio.to_thread(self._recorder.start, device_id=device_id)
            self._active_ingestion_id = ingestion_id
            self._voice_mode = DesktopVoiceMode.NOTE
            return ingestion_id

    async def stop_and_process(self) -> DesktopProcessingResult:
        async with self._operation_lock:
            self._require_started()
            ingestion_id = self._active_ingestion_id
            if ingestion_id is None or self._voice_mode is not DesktopVoiceMode.NOTE:
                raise DesktopStateError

            artifact = None
            self._active_ingestion_id = None
            self._voice_mode = None
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
        if self._operation_lock.locked():
            raise DesktopStateError
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
        """Cancel generation before taking the lock; never wait for its 180s timeout."""

        self._closing = True
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._shutdown_task)
            raise

    async def _shutdown(self) -> None:
        tasks = {task for task in (self._rag_task, self._voice_question_task)
                 if task is not None and not task.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._active_ingestion_id = None
            self._voice_mode = None
            self._pending = None
            await self._shutdown_resources_unlocked()

    async def ask_memory(self, *, query: str, top_k: int = 5) -> DesktopRagResult:
        """Read-only text question; no recorder/STT/ingestion/indexing/recent writes."""

        try:
            MemoryRetrievalService.validate_input(query=query, top_k=top_k)
        except MemoryRetrievalError:
            raise DesktopRagInputError from None
        if self._operation_lock.locked():
            raise DesktopStateError
        async with self._operation_lock:
            self._require_started()
            if (self._voice_mode is not None or self._pending is not None or self._rag is None
                    or (self._voice_question_task is not None
                        and self._voice_question_task is not asyncio.current_task())):
                raise DesktopStateError
            self._rag_task = asyncio.current_task()
            try:
                self._progress_callback(DesktopRagProgress(DesktopRagProgressState.RETRIEVING))
                def generating() -> None:
                    self._progress_callback(DesktopRagProgress(DesktopRagProgressState.GENERATING))
                result = await self._rag.ask(query=query, top_k=top_k, on_generating=generating)
                return DesktopRagResult(
                    status=(DesktopRagStatus.ANSWERED if result.sufficient_context
                            else DesktopRagStatus.INSUFFICIENT_CONTEXT),
                    answer=result.answer if result.sufficient_context else INSUFFICIENT_CONTEXT_ANSWER,
                    citations=tuple(DesktopRagCitation(c.memory_id, c.rank, c.score)
                                    for c in result.citations) if result.sufficient_context else (),
                )
            except MemoryRagError as exc:
                return DesktopRagResult(DesktopRagStatus.FAILED, "", error_message=_rag_error_message(exc.code))
            except Exception:
                return DesktopRagResult(DesktopRagStatus.FAILED, "",
                                        error_message="An answer could not be generated.")
            finally:
                self._rag_task = None

    async def start_voice_question(self, *, device_id: int) -> None:
        """Explicit question capture: no ingestion identity, no new provider."""

        if self._operation_lock.locked():
            raise DesktopStateError
        async with self._operation_lock:
            self._require_started()
            if (self._pending is not None or self._voice_mode is not None
                    or self._voice_question_task is not None):
                raise DesktopStateError
            try:
                await _finish_audio_before_cancellation(asyncio.create_task(
                    asyncio.to_thread(self._recorder.start, device_id=device_id)))
            except asyncio.CancelledError:
                await _finish_audio_before_cancellation(asyncio.create_task(
                    self._discard_question_capture()))
                raise
            except Exception:
                raise DesktopRecordingError from None
            self._voice_mode = DesktopVoiceMode.QUESTION

    async def stop_voice_question_and_ask(self, *, top_k: int = 5) -> DesktopVoiceQuestionResult:
        """Drain protected audio work, then call the existing public text ASK."""

        try:
            MemoryRetrievalService.validate_input(query="voice question", top_k=top_k)
        except MemoryRetrievalError:
            raise DesktopRagInputError from None
        if self._operation_lock.locked():
            raise DesktopStateError
        task = asyncio.current_task()
        try:
            async with self._operation_lock:
                self._require_started()
                if self._voice_mode is not DesktopVoiceMode.QUESTION:
                    raise DesktopStateError
                self._voice_question_task = task
                self._progress_callback(DesktopVoiceQuestionProgress(
                    DesktopVoiceQuestionProgressState.TRANSCRIBING))
                transcription = await _finish_audio_before_cancellation(asyncio.create_task(
                    self._transcribe_question_audio()))
                self._voice_mode = None
                self._progress_callback(DesktopVoiceQuestionProgress(
                    DesktopVoiceQuestionProgressState.TRANSCRIPT_READY, transcription.text))
            if not transcription.text.strip():
                return DesktopVoiceQuestionResult(transcription.text)
            if self._closing:
                raise asyncio.CancelledError
            result = await self.ask_memory(query=transcription.text, top_k=top_k)
            return DesktopVoiceQuestionResult(transcription.text, result)
        finally:
            if self._voice_question_task is task:
                self._voice_question_task = None
                self._voice_mode = None

    async def _transcribe_question_audio(self) -> TranscriptionResult:
        artifact = None
        try:
            artifact = await asyncio.to_thread(self._recorder.stop)
            service = self._transcription_service
            if service is None:
                raise DesktopStateError
            return await service.transcribe(request=TranscriptionRequest(
                audio=AudioPathInput(path=artifact.path), mode=TranscriptionMode.TRANSCRIBE,
                timestamp_mode=TranscriptionTimestampMode.NONE))
        except Exception:
            raise DesktopVoiceQuestionTranscriptionError from None
        finally:
            if artifact is not None:
                # This task is shielded until Whisper's thread has finished reading.
                try:
                    await asyncio.to_thread(self._recorder.cleanup, artifact)
                except Exception:
                    raise DesktopVoiceQuestionTranscriptionError from None

    async def _discard_question_capture(self) -> None:
        try:
            artifact = await asyncio.to_thread(self._recorder.stop)
        except (DesktopStateError, DesktopRecordingError):
            return
        await asyncio.to_thread(self._recorder.cleanup, artifact)

    async def reconcile_missing_indexes(self) -> IndexReconciliationReport:
        async with self._operation_lock:
            self._require_started()
            if self._voice_mode is not None or self._voice_question_task is not None:
                raise DesktopStateError
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
        if not self._started or self._closed or self._closing:
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
        except (MemoryArchivedError, MemoryIdempotencyConflictError) as exc:
            self._pending = None
            return DesktopProcessingResult(
                status=(DesktopResultStatus.INGESTION_ARCHIVED if isinstance(exc, MemoryArchivedError)
                        else DesktopResultStatus.INGESTION_CONFLICT),
                transcript=pending.transcription.text, memory_id=None, save_pending=False,
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
        cancellation = None
        rag, self._rag = self._rag, None
        if rag is not None:
            try:
                await rag.aclose()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                pass
        indexer = self._indexer
        self._indexer = None
        if indexer is not None:
            try:
                await indexer.aclose()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                pass
        embedding, self._embedding_provider = self._embedding_provider, None
        if embedding is not None:
            try:
                await embedding.aclose()
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


def _rag_error_message(code: str) -> str:
    if code == "rag.retrieval_failed":
        return "Memory search could not be completed."
    if code == "rag.invalid_configuration":
        return "Memory assistant configuration is invalid."
    return "An answer could not be generated."


async def _finish_audio_before_cancellation(task: asyncio.Task[Any]) -> Any:
    """Never unlink audio or release resources while thread-backed STT uses it."""

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Consume a possible failure; the caller's cancellation has precedence.
        if not task.cancelled():
            task.exception()
        raise
