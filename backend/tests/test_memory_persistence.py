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
        self.statements: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def execute(self, statement: object, parameters=None) -> object:
        self.statements.append(statement)
        return self.execute_results.pop(0)


def _scalar_result(value=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _row(memory: Memory) -> MemoryDb:
    return MemoryDb(
        id=memory.id,
        ingestion_id=memory.ingestion_id,
        content=memory.content,
        source_kind=memory.source_kind.value,
        session_id=memory.session_id,
        metadata_json=dict(memory.metadata),
        created_at=memory.created_at, revision=memory.revision, archived_at=memory.archived_at,
    )


def test_repository_creates_and_flushes_without_owning_transaction() -> None:
    async def scenario() -> None:
        fake = FakeSession()
        repository = PostgresMemoryRepository(db=cast(AsyncSession, fake))
        memory = Memory(content="repozytorium", metadata={"language": "pl"})
        fake.execute_results = [_scalar_result(), _scalar_result()]

        result = await repository.create(memory)

        assert result == memory
        assert fake.flush_count == 1
        assert fake.commit_count == 0
        assert fake.rollback_count == 0
        assert len(fake.added) == 1
        stored = cast(MemoryDb, fake.added[0])
        assert stored.id == memory.id
        assert stored.ingestion_id == memory.ingestion_id
        assert stored.metadata_json == {"language": "pl"}

    asyncio.run(scenario())


def test_repository_atomically_creates_or_returns_ingestion_owner() -> None:
    async def scenario() -> None:
        fake = FakeSession()
        repository = PostgresMemoryRepository(db=cast(AsyncSession, fake))
        memory = Memory(content="idempotentna pamięć")

        inserted_result = MagicMock()
        inserted_result.scalar_one_or_none.return_value = _row(memory)
        fake.execute_results = [_scalar_result(), _scalar_result(), inserted_result]
        created = await repository.create_or_get_by_ingestion_id(memory)

        conflict_result = MagicMock()
        conflict_result.scalar_one_or_none.return_value = None
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = _row(memory)
        fake.execute_results = [_scalar_result(), _scalar_result(), conflict_result, existing_result]
        duplicate = await repository.create_or_get_by_ingestion_id(
            Memory(ingestion_id=memory.ingestion_id, content=memory.content)
        )

        assert created.created is True
        assert created.memory == memory
        assert duplicate.created is False
        assert duplicate.memory == memory
        assert fake.commit_count == 0
        assert fake.rollback_count == 0

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


@pytest.mark.parametrize("present", [True, False])
def test_repository_deletes_by_id_without_owning_transaction(present):
    async def scenario():
        from sqlalchemy.dialects import postgresql

        fake = FakeSession()
        memory = Memory(content="synthetic deletion")
        values = (
            (memory.ingestion_id, None, memory.ingestion_id, memory.id, memory.id)
            if present else (None, None)
        )
        fake.execute_results = [_scalar_result(value) for value in values]
        repository = PostgresMemoryRepository(db=cast(AsyncSession, fake))
        assert await repository.delete_by_id(memory.id) is present
        compiled = [str(statement.compile(dialect=postgresql.dialect()))
                    for statement in fake.statements]
        if present:
            assert "pg_advisory_xact_lock" in compiled[1]
            assert "FOR UPDATE" in compiled[2]
            assert compiled[3].startswith("INSERT INTO memory_ingestion_tombstones")
            assert compiled[4].startswith("DELETE FROM memories")
        else:
            assert all("INSERT INTO" not in sql and "DELETE FROM" not in sql for sql in compiled)
        assert fake.commit_count == fake.rollback_count == 0
    asyncio.run(scenario())


def test_repository_wraps_internal_failure_without_exposing_details() -> None:
    async def scenario() -> None:
        db = MagicMock(spec=AsyncSession)
        db.flush = AsyncMock(side_effect=RuntimeError("password=top-secret"))
        db.execute = AsyncMock(side_effect=[_scalar_result(), _scalar_result()])
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
        "ingestion_id",
        "content",
        "revision",
        "archived_at",
        "source_kind",
        "session_id",
        "metadata_json",
        "created_at",
    ]
    assert table.c.id.primary_key is True
    assert table.c.ingestion_id.nullable is False
    assert table.c.session_id.nullable is True
    assert table.c.metadata_json.nullable is False
    assert table.c.created_at.nullable is False
    assert any(
        constraint.name == "uq_memories_ingestion_id"
        for constraint in table.constraints
    )
    memory_registration.assert_called_once_with()
    vector_registration.assert_called_once_with(dimension=1024)


def test_alembic_graph_has_one_host_head() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["kulai_memory_0004"]
    tombstone_revision = scripts.get_revision("kulai_memory_0003")
    assert tombstone_revision is not None
    assert tombstone_revision.down_revision == "kulai_memory_0002"
    revision = scripts.get_revision("kulai_memory_0002")
    assert revision is not None
    assert revision.down_revision == "kulai_memory_0001"
    initial = scripts.get_revision("kulai_memory_0001")
    assert initial is not None
    assert initial.down_revision == "kvectorstorepg_0001"
