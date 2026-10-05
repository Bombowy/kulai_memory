from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from math import inf, nan
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kulai_memory.application import (
    IdempotentMemoryWrite,
    Memory,
    MemoryRepository,
    MemoryService,
    MemorySourceKind,
)


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, Memory] = {}
        self.created: list[Memory] = []
        self.list_limits: list[int] = []

    async def create(self, memory: Memory) -> Memory:
        self.records[memory.id] = memory
        self.created.append(memory)
        return memory

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        existing = next(
            (
                record
                for record in self.records.values()
                if record.ingestion_id == memory.ingestion_id
            ),
            None,
        )
        if existing is not None:
            return IdempotentMemoryWrite(memory=existing, created=False)
        await self.create(memory)
        return IdempotentMemoryWrite(memory=memory, created=True)

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        return self.records.get(memory_id)

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        self.list_limits.append(limit)
        ordered = sorted(
            self.records.values(),
            key=lambda memory: (memory.created_at, memory.id),
            reverse=True,
        )
        return tuple(ordered[:limit])


def test_memory_defaults_have_stable_identity_and_utc_timestamp() -> None:
    memory = Memory(content="  zachowaj treść transkryptu  ")
    another = Memory(content="inna pamięć")

    assert isinstance(memory.id, UUID)
    assert isinstance(memory.ingestion_id, UUID)
    assert memory.id == memory.id
    assert memory.id != another.id
    assert memory.ingestion_id != another.ingestion_id
    assert memory.content == "  zachowaj treść transkryptu  "
    assert memory.source_kind is MemorySourceKind.VOICE
    assert memory.session_id is None
    assert memory.metadata == {}
    assert memory.created_at.tzinfo is UTC


@pytest.mark.parametrize("content", ["", " ", "\t\r\n"])
def test_memory_rejects_empty_content(content: str) -> None:
    with pytest.raises(ValidationError):
        Memory(content=content)


def test_memory_validates_json_metadata_and_normalizes_time_to_utc() -> None:
    session_id = uuid4()
    memory = Memory(
        content="spotkanie jutro",
        source_kind="voice",
        session_id=session_id,
        metadata={"labels": ("work", "calendar"), "confidence": 0.9},
        created_at=datetime.fromisoformat("2026-10-02T12:00:00+02:00"),
    )

    assert memory.source_kind is MemorySourceKind.VOICE
    assert memory.session_id == session_id
    assert memory.metadata == {
        "labels": ["work", "calendar"],
        "confidence": 0.9,
    }
    assert memory.created_at == datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    json.dumps(memory.model_dump(mode="json"))


@pytest.mark.parametrize(
    "metadata",
    [
        {"invalid": object()},
        {"invalid": datetime.now(UTC)},
        {"invalid": inf},
        {"invalid": nan},
        {1: "non-string-key"},
    ],
)
def test_memory_rejects_non_json_metadata(metadata: object) -> None:
    with pytest.raises(ValidationError):
        Memory(content="valid", metadata=metadata)  # type: ignore[arg-type]


def test_memory_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError):
        Memory(content="valid", created_at=datetime(2026, 10, 2))


def test_memory_service_delegates_to_repository() -> None:
    async def scenario() -> None:
        repository = InMemoryMemoryRepository()
        service = MemoryService(repository=repository)
        session_id = uuid4()

        created = await service.create_memory(
            content="zapamiętaj tę informację",
            source_kind="voice",
            session_id=session_id,
            metadata={"language": "pl"},
        )
        fetched = await service.get_memory(created.id)
        recent = await service.list_recent_memories(limit=10)

        assert isinstance(repository, MemoryRepository)
        assert repository.created == [created]
        assert fetched == created
        assert recent == (created,)
        assert repository.list_limits == [10]
        assert created.session_id == session_id

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, 101, True])
def test_memory_service_rejects_invalid_list_limit(limit: int) -> None:
    async def scenario() -> None:
        service = MemoryService(repository=InMemoryMemoryRepository())
        with pytest.raises(ValueError, match="between 1 and 100"):
            await service.list_recent_memories(limit=limit)

    asyncio.run(scenario())
