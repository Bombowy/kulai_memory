from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from kulai_db import Base
from sqlalchemy.ext.asyncio import AsyncSession

from kulai_memory import orm_models
from kulai_memory.application import Memory, MemoryPersistenceError
from kulai_memory.persistence import MemoryDb, PostgresMemoryRepository


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.execute_results: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def execute(self, statement: object) -> object:
        del statement
        return self.execute_results.pop(0)


def _row(memory: Memory) -> MemoryDb:
    return MemoryDb(
        id=memory.id,
        content=memory.content,
        source_kind=memory.source_kind.value,
        session_id=memory.session_id,
        metadata_json=dict(memory.metadata),
        created_at=memory.created_at,
    )


def test_repository_creates_and_flushes_without_owning_transaction() -> None:
    async def scenario() -> None:
        fake = FakeSession()
        repository = PostgresMemoryRepository(db=cast(AsyncSession, fake))
        memory = Memory(content="repozytorium", metadata={"language": "pl"})

        result = await repository.create(memory)

        assert result == memory
        assert fake.flush_count == 1
        assert fake.commit_count == 0
        assert fake.rollback_count == 0
        assert len(fake.added) == 1
        stored = cast(MemoryDb, fake.added[0])
        assert stored.id == memory.id
        assert stored.metadata_json == {"language": "pl"}

    asyncio.run(scenario())


def test_repository_maps_get_and_recent_rows() -> None:
    async def scenario() -> None:
        fake = FakeSession()
        older = Memory(
            content="starsza",
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        newer = Memory(
            content="nowsza",
            created_at=datetime(2026, 10, 2, tzinfo=UTC),
        )
        one_result = MagicMock()
        one_result.scalar_one_or_none.return_value = _row(newer)
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = [_row(newer), _row(older)]
        fake.execute_results = [one_result, list_result]
        repository = PostgresMemoryRepository(db=cast(AsyncSession, fake))

        fetched = await repository.get_by_id(newer.id)
        recent = await repository.list_recent(limit=2)

        assert fetched == newer
        assert recent == (newer, older)
        assert fake.commit_count == 0
        assert fake.rollback_count == 0

    asyncio.run(scenario())


def test_repository_wraps_internal_failure_without_exposing_details() -> None:
    async def scenario() -> None:
        db = MagicMock(spec=AsyncSession)
        db.flush = AsyncMock(side_effect=RuntimeError("password=top-secret"))
        repository = PostgresMemoryRepository(db=db)

        with pytest.raises(MemoryPersistenceError) as caught:
            await repository.create(Memory(content="valid"))

        assert str(caught.value) == MemoryPersistenceError.safe_message
        assert "password" not in str(caught.value)
        assert isinstance(caught.value.__cause__, RuntimeError)
        db.commit.assert_not_called()
        db.rollback.assert_not_called()

    asyncio.run(scenario())


def test_memory_orm_schema_and_host_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    vector_registration = MagicMock()
    memory_registration = MagicMock(return_value=MemoryDb)
    monkeypatch.setattr(
        orm_models,
        "require_deployment_settings",
        lambda: SimpleNamespace(kulai_vector_dimension=1024),
    )
    monkeypatch.setattr(
        orm_models,
        "register_vector_store_pgvector_models",
        vector_registration,
    )
    monkeypatch.setattr(
        orm_models,
        "register_memory_orm_models",
        memory_registration,
    )

    orm_models.register_orm_models()

    assert "memories" in Base.metadata.tables
    table = Base.metadata.tables["memories"]
    assert list(table.columns.keys()) == [
        "id",
        "content",
        "source_kind",
        "session_id",
        "metadata_json",
        "created_at",
    ]
    assert table.c.id.primary_key is True
    assert table.c.session_id.nullable is True
    assert table.c.metadata_json.nullable is False
    assert table.c.created_at.nullable is False
    memory_registration.assert_called_once_with()
    vector_registration.assert_called_once_with(dimension=1024)


def test_alembic_graph_has_one_host_head() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["kulai_memory_0001"]
    revision = scripts.get_revision("kulai_memory_0001")
    assert revision is not None
    assert revision.down_revision == "kvectorstorepg_0001"
