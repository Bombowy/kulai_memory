from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from kulai_transcription import (
    TranscriptionLanguage,
    TranscriptionLanguageSource,
    TranscriptionMode,
    TranscriptionResult,
    TranscriptionSegment,
)

from kulai_memory.application import (
    IdempotentMemoryWrite,
    Memory,
    MemoryIdempotencyConflictError,
    MemoryService,
    TranscriptMemoryIngestionService,
    TranscriptMemoryIngestionStatus,
)


class InMemoryIdempotentRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, Memory] = {}
        self.by_ingestion_id: dict[UUID, Memory] = {}
        self.idempotent_calls = 0

    async def create(self, memory: Memory) -> Memory:
        self.records[memory.id] = memory
        self.by_ingestion_id[memory.ingestion_id] = memory
        return memory

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        self.idempotent_calls += 1
        existing = self.by_ingestion_id.get(memory.ingestion_id)
        if existing is not None:
            return IdempotentMemoryWrite(memory=existing, created=False)
        await self.create(memory)
        return IdempotentMemoryWrite(memory=memory, created=True)

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        return self.records.get(memory_id)

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        return tuple(self.records.values())[:limit]


def _transcription(
    text: str,
    *,
    provider_id: str = "fake-stt",
    model_id: str | None = "large-v3",
    duration_seconds: float | None = 1.25,
    language_code: str = "pl",
    segment_text: str | None = "segment content",
) -> TranscriptionResult:
    segments = (
        ()
        if segment_text is None
        else (TranscriptionSegment(text=segment_text, segment_id="0"),)
    )
    return TranscriptionResult(
        text=text,
        provider_id=provider_id,
        model_id=model_id,
        mode=TranscriptionMode.TRANSCRIBE,
        language=TranscriptionLanguage(
            code=language_code,
            source=TranscriptionLanguageSource.DETECTED,
            confidence=0.91,
        ),
        segments=segments,
        duration_seconds=duration_seconds,
        confidence=0.88,
    )


def _service(repository: InMemoryIdempotentRepository) -> TranscriptMemoryIngestionService:
    return TranscriptMemoryIngestionService(
        memory_service=MemoryService(repository=repository)
    )


@pytest.mark.parametrize("text", ["", " ", "\t\r\n"])
def test_empty_transcript_is_skipped_without_repository_call(text: str) -> None:
    async def scenario() -> None:
        repository = InMemoryIdempotentRepository()
        result = await _service(repository).ingest(
            transcription=_transcription(text, language_code="en", segment_text=None),
            ingestion_id=uuid4(),
            session_id=uuid4(),
        )

        assert result.status is TranscriptMemoryIngestionStatus.SKIPPED_EMPTY
        assert result.memory is None
        assert repository.idempotent_calls == 0
        assert repository.records == {}

    asyncio.run(scenario())


def test_nonempty_transcript_creates_memory_with_safe_provider_neutral_metadata() -> None:
    async def scenario() -> None:
        repository = InMemoryIdempotentRepository()
        ingestion_id = uuid4()
        session_id = uuid4()
        transcript = "  zapamiętaj oryginalny tekst  "
        result = await _service(repository).ingest(
            transcription=_transcription(
                transcript,
                language_code="nn",
                segment_text="private segment",
            ),
            ingestion_id=ingestion_id,
            session_id=session_id,
        )

        assert result.status is TranscriptMemoryIngestionStatus.CREATED
        assert result.memory is not None
        assert result.memory.ingestion_id == ingestion_id
        assert result.memory.session_id == session_id
        assert result.memory.content == transcript
        assert result.memory.metadata == {
            "transcription": {
                "provider_id": "fake-stt",
                "model_id": "large-v3",
                "mode": "transcribe",
                "language": {
                    "code": "nn",
                    "source": "detected",
                    "confidence": 0.91,
                },
                "duration_seconds": 1.25,
                "confidence": 0.88,
                "segment_count": 1,
            }
        }
        assert "private segment" not in str(result.memory.metadata)

    asyncio.run(scenario())


def test_same_ingestion_id_returns_first_memory_without_replacing_metadata() -> None:
    async def scenario() -> None:
        repository = InMemoryIdempotentRepository()
        service = _service(repository)
        ingestion_id = uuid4()
        session_id = uuid4()

        first = await service.ingest(
            transcription=_transcription("ta sama treść"),
            ingestion_id=ingestion_id,
            session_id=session_id,
        )
        duplicate = await service.ingest(
            transcription=_transcription(
                "ta sama treść",
                provider_id="retry-provider",
                duration_seconds=2.0,
            ),
            ingestion_id=ingestion_id,
            session_id=session_id,
        )

        assert first.status is TranscriptMemoryIngestionStatus.CREATED
        assert duplicate.status is TranscriptMemoryIngestionStatus.DUPLICATE
        assert duplicate.memory == first.memory
        assert duplicate.memory is not None
        assert duplicate.memory.metadata["transcription"]["provider_id"] == "fake-stt"
        assert len(repository.records) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["content", "session"])
def test_reused_ingestion_id_with_different_semantic_payload_conflicts(
    change: str,
) -> None:
    async def scenario() -> None:
        repository = InMemoryIdempotentRepository()
        service = _service(repository)
        ingestion_id = uuid4()
        session_id = uuid4()
        await service.ingest(
            transcription=_transcription("pierwsza treść"),
            ingestion_id=ingestion_id,
            session_id=session_id,
        )

        with pytest.raises(MemoryIdempotencyConflictError) as caught:
            await service.ingest(
                transcription=_transcription(
                    "inna treść" if change == "content" else "pierwsza treść"
                ),
                ingestion_id=ingestion_id,
                session_id=uuid4() if change == "session" else session_id,
            )

        assert str(caught.value) == MemoryIdempotencyConflictError.safe_message
        assert len(repository.records) == 1

    asyncio.run(scenario())


def test_same_transcript_with_different_ingestion_ids_creates_two_memories() -> None:
    async def scenario() -> None:
        repository = InMemoryIdempotentRepository()
        service = _service(repository)
        session_id = uuid4()
        first = await service.ingest(
            transcription=_transcription("powtórzona prawdziwa wypowiedź"),
            ingestion_id=uuid4(),
            session_id=session_id,
        )
        second = await service.ingest(
            transcription=_transcription("powtórzona prawdziwa wypowiedź"),
            ingestion_id=uuid4(),
            session_id=session_id,
        )

        assert first.status is TranscriptMemoryIngestionStatus.CREATED
        assert second.status is TranscriptMemoryIngestionStatus.CREATED
        assert first.memory is not None and second.memory is not None
        assert first.memory.id != second.memory.id
        assert len(repository.records) == 2

    asyncio.run(scenario())
