"""Small desktop-facing value objects without Qt or infrastructure coupling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from uuid import UUID

from kulai_memory.application.indexing import IndexReconciliationReport


class DesktopResultStatus(str, Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    SKIPPED_EMPTY = "skipped_empty"
    SAVE_FAILED = "save_failed"
    INDEXING_FAILED = "indexing_failed"
    INGESTION_RETIRED = "ingestion_retired"
    INGESTION_ARCHIVED = "ingestion_archived"
    INGESTION_CONFLICT = "ingestion_conflict"


class DesktopProgressState(str, Enum):
    TRANSCRIBING = "transcribing"
    TRANSCRIPT_READY = "transcript_ready"
    SAVING = "saving"
    INDEXING = "indexing"


class DesktopRagStatus(str, Enum):
    ANSWERED = "answered"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    FAILED = "failed"


class DesktopRagProgressState(str, Enum):
    RETRIEVING = "retrieving"
    GENERATING = "generating"


class DesktopVoiceMode(str, Enum):
    NOTE = "note"
    QUESTION = "question"


class DesktopVoiceQuestionProgressState(str, Enum):
    TRANSCRIBING = "transcribing"
    TRANSCRIPT_READY = "transcript_ready"


@dataclass(frozen=True, slots=True)
class DesktopVoiceQuestionProgress:
    state: DesktopVoiceQuestionProgressState
    transcript: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopRagProgress:
    state: DesktopRagProgressState


@dataclass(frozen=True, slots=True)
class DesktopRagCitation:
    memory_id: UUID
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class DesktopRagResult:
    status: DesktopRagStatus
    answer: str
    citations: tuple[DesktopRagCitation, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopVoiceQuestionResult:
    """No RAG result means no speech; never contains audio or hidden context."""

    transcript: str
    rag_result: DesktopRagResult | None = None


@dataclass(frozen=True, slots=True)
class MicrophoneDevice:
    device_id: int
    name: str
    host_api: str | None
    is_default: bool

    @property
    def display_name(self) -> str:
        host = f" - {self.host_api}" if self.host_api else ""
        marker = " (default)" if self.is_default else ""
        return f"{self.name}{host} [{self.device_id}]{marker}"


@dataclass(frozen=True, slots=True)
class RecordingArtifact:
    path: Path
    duration_seconds: float
    limit_reached: bool


@dataclass(frozen=True, slots=True)
class MemorySummary:
    id: UUID
    created_at: datetime
    content: str


@dataclass(frozen=True, slots=True)
class DesktopStartupResult:
    devices: tuple[MicrophoneDevice, ...]
    memories: tuple[MemorySummary, ...]
    indexing: IndexReconciliationReport = IndexReconciliationReport()


@dataclass(frozen=True, slots=True)
class DesktopProgress:
    state: DesktopProgressState
    transcript: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopProcessingResult:
    status: DesktopResultStatus
    transcript: str
    memory_id: UUID | None
    save_pending: bool


class DesktopPublicError(RuntimeError):
    safe_message = "The desktop operation could not be completed."

    def __init__(self) -> None:
        self.public_message = self.safe_message
        super().__init__(self.public_message)


class DesktopConfigurationError(DesktopPublicError):
    safe_message = "Desktop configuration is invalid."


class DesktopDependencyError(DesktopPublicError):
    safe_message = "Install the desktop optional dependencies."


class DesktopDatabaseError(DesktopPublicError):
    safe_message = "Database is unavailable or its schema is not current."


class DesktopRecordingError(DesktopPublicError):
    safe_message = "Microphone recording could not be completed."


class DesktopStateError(DesktopPublicError):
    safe_message = "The desktop operation is not allowed in the current state."


class DesktopTranscriptionError(DesktopPublicError):
    safe_message = "Transcription failed."


class DesktopRagInputError(DesktopPublicError):
    safe_message = "Enter a nonblank question of at most 10000 characters and top-k between 1 and 20."


class DesktopRagConfigurationError(DesktopPublicError):
    safe_message = "Memory assistant configuration is invalid."


class DesktopVoiceQuestionTranscriptionError(DesktopPublicError):
    safe_message = "Question transcription failed."
