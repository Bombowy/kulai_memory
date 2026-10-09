"""Transport-neutral Memory domain and application service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from .ports import MemoryRepository


class MemorySourceKind(str, Enum):
    """Semantic origin of a memory, independent of client transport."""

    VOICE = "voice"


def _validated_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("Metadata numbers must be finite.")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Metadata object keys must be strings.")
            result[key] = _validated_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_validated_json(item) for item in value]
    raise ValueError("Metadata values must be JSON-compatible.")


class Memory(BaseModel):
    """One durable user memory before embedding or classification."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    id: UUID = Field(default_factory=uuid4)
    ingestion_id: UUID = Field(default_factory=uuid4)
    content: str
    source_kind: MemorySourceKind = MemorySourceKind.VOICE
    session_id: UUID | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision: int = Field(default=1, ge=1, strict=True)
    archived_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Memory content must not be empty.")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("Memory metadata must be a JSON object.")
        validated = _validated_json(value)
        if not isinstance(validated, dict):
            raise ValueError("Memory metadata must be a JSON object.")
        return validated

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Memory created_at must be timezone-aware.")
        return value.astimezone(UTC)

    @field_validator("archived_at")
    @classmethod
    def validate_archived_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Memory archived_at must be timezone-aware.")
        return value.astimezone(UTC)


class MemoryPersistenceError(RuntimeError):
    """A safe application error for a failed repository operation."""

    safe_message = "The memory operation could not be completed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class MemoryIdempotencyConflictError(RuntimeError):
    """The ingestion key was already used for a different voice memory."""

    safe_message = "The ingestion identifier conflicts with an existing memory."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class MemoryIngestionRetiredError(RuntimeError):
    """A deleted ingestion identity cannot create a Memory again."""

    code = "memory.ingestion_retired"
    safe_message = "This note was deleted and cannot be saved again."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class MemoryArchivedError(RuntimeError):
    code = "memory.archived"
    safe_message = "This memory is archived. Restore it before making changes."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


@dataclass(frozen=True, slots=True)
class IdempotentMemoryWrite:
    """Result of one atomic create-or-get repository operation."""

    memory: Memory
    created: bool


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Memory list limit must be between 1 and 100.")
    return limit


class MemoryService:
    """Application boundary for creating and reading memories."""

    def __init__(self, *, repository: MemoryRepository) -> None:
        self._repository = repository

    async def create_memory(
        self,
        *,
        content: str,
        ingestion_id: UUID | None = None,
        source_kind: MemorySourceKind | str = MemorySourceKind.VOICE,
        session_id: UUID | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Memory:
        memory = Memory(
            ingestion_id=ingestion_id if ingestion_id is not None else uuid4(),
            content=content,
            source_kind=source_kind,
            session_id=session_id,
            metadata=dict(metadata) if metadata is not None else {},
        )
        return await self._repository.create(memory)

    async def create_memory_idempotent(
        self,
        *,
        ingestion_id: UUID,
        content: str,
        source_kind: MemorySourceKind | str = MemorySourceKind.VOICE,
        session_id: UUID | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> IdempotentMemoryWrite:
        """Create once for an ingestion UUID or return its existing Memory."""

        candidate = Memory(
            ingestion_id=ingestion_id,
            content=content,
            source_kind=source_kind,
            session_id=session_id,
            metadata=dict(metadata) if metadata is not None else {},
        )
        write = await self._repository.create_or_get_by_ingestion_id(candidate)
        existing = write.memory
        if existing.archived_at is not None:
            raise MemoryArchivedError()
        if not write.created and (
            existing.content != candidate.content
            or existing.source_kind != candidate.source_kind
            or existing.session_id != candidate.session_id
        ):
            raise MemoryIdempotencyConflictError()
        return write

    async def get_memory(self, memory_id: UUID) -> Memory | None:
        return await self._repository.get_by_id(memory_id)

    async def list_recent_memories(self, *, limit: int = 50) -> tuple[Memory, ...]:
        return await self._repository.list_recent(limit=_validate_limit(limit))
