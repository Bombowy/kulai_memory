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
class DesktopMemoryItem:
    id: UUID
    created_at: datetime
    revision: int
    archived: bool
    content: str


class DesktopMemoryFilter(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class DesktopMemoryChangeStatus(str, Enum):
    EDITED = "edited"
    UNCHANGED = "unchanged"
    ARCHIVED = "archived"
    ALREADY_ARCHIVED = "already_archived"
    RESTORED = "restored"
    ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True, slots=True)
class DesktopMemoryChangeResult:
    item: DesktopMemoryItem
    status: DesktopMemoryChangeStatus
    indexing_degraded: bool = False


class DesktopDeleteProgressState(str, Enum):
    PREPARING_BACKUP = "preparing_backup"
    VERIFYING_BACKUP = "verifying_backup"
    DELETING = "deleting"


@dataclass(frozen=True, slots=True)
class DesktopDeleteProgress:
    state: DesktopDeleteProgressState


@dataclass(frozen=True, slots=True)
class DesktopMemoryDeleteResult:
    memory_id: UUID
    backup_path: Path
    backup_size: int
    backup_sha256: str
    memory_deleted: bool
    vector_deleted_count: int


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


class DesktopLibraryInputError(DesktopPublicError):
    safe_message = "Choose Active or Archived and a library limit between 1 and 100."


class DesktopLibraryReadError(DesktopPublicError):
    safe_message = "Memory Library could not be loaded."


class DesktopMemoryChangeError(DesktopPublicError):
    safe_message = "The memory change could not be completed."


class DesktopMemoryEditInputError(DesktopPublicError):
    safe_message = "Enter nonblank memory content and a valid revision."


class DesktopMemoryConflictError(DesktopPublicError):
    safe_message = "Memory changed. Reload it before editing."


class DesktopMemoryArchivedError(DesktopPublicError):
    safe_message = "Archived memories cannot be edited."


class DesktopDeleteInputError(DesktopPublicError):
    safe_message = "Select a memory with a valid revision and choose a new .dump backup outside the repository."


class DesktopBackupError(DesktopPublicError):
    safe_message = "Backup verification could not be completed. Memory was not deleted; retain any backup file."


class DesktopDeleteError(DesktopPublicError):
    safe_message = "Memory deletion could not be completed. Retain the backup file."


class DesktopDeleteConflictError(DesktopPublicError):
    safe_message = "Memory changed. Reload it before deleting."
