"""Provider-neutral transcription-to-Memory ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from kulai_transcription import TranscriptionResult
from pydantic import JsonValue

from .memory import Memory, MemoryService


class TranscriptMemoryIngestionStatus(str, Enum):
    """Observable outcome of one transcript ingestion request."""

    CREATED = "created"
    DUPLICATE = "duplicate"
    SKIPPED_EMPTY = "skipped_empty"


@dataclass(frozen=True, slots=True)
class TranscriptMemoryIngestionResult:
    """Memory outcome without imposing a transport event contract."""

    status: TranscriptMemoryIngestionStatus
    memory: Memory | None


def transcription_is_empty(result: TranscriptionResult) -> bool:
    """Return whether the canonical transcript contains no user content."""

    return not result.text.strip()


def _transcription_metadata(result: TranscriptionResult) -> dict[str, JsonValue]:
    language: JsonValue = None
    if result.language is not None:
        language = {
            "code": result.language.code,
            "source": (
                result.language.source.value
                if result.language.source is not None
                else None
            ),
            "confidence": result.language.confidence,
        }

    return {
        "transcription": {
            "provider_id": result.provider_id,
            "model_id": result.model_id,
            "mode": result.mode.value,
            "language": language,
            "duration_seconds": result.duration_seconds,
            "confidence": result.confidence,
            "segment_count": len(result.segments),
        }
    }


class TranscriptMemoryIngestionService:
    """Apply the explicit transcript acceptance rule and persist once."""

    def __init__(self, *, memory_service: MemoryService) -> None:
        self._memory_service = memory_service

    async def ingest(
        self,
        *,
        transcription: TranscriptionResult,
        ingestion_id: UUID,
        session_id: UUID,
    ) -> TranscriptMemoryIngestionResult:
        if transcription_is_empty(transcription):
            return TranscriptMemoryIngestionResult(
                status=TranscriptMemoryIngestionStatus.SKIPPED_EMPTY,
                memory=None,
            )

        write = await self._memory_service.create_memory_idempotent(
            ingestion_id=ingestion_id,
            content=transcription.text,
            session_id=session_id,
            metadata=_transcription_metadata(transcription),
        )
        status = (
            TranscriptMemoryIngestionStatus.CREATED
            if write.created
            else TranscriptMemoryIngestionStatus.DUPLICATE
        )
        return TranscriptMemoryIngestionResult(status=status, memory=write.memory)
