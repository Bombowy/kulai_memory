from __future__ import annotations

import asyncio
import hashlib
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from kulai_db import Base
from sqlalchemy.dialects import postgresql

from kulai_memory.application import Memory, MemoryIngestionRetiredError, MemoryService
from kulai_memory.database_safety import _tombstone_schema_checks
from kulai_memory.persistence import MemoryIngestionTombstoneDb, PostgresMemoryRepository
from kulai_memory.persistence.ingestion_lock import ingestion_lock_key


IDENTITY = UUID("45b6bcd2-457e-4b30-a24a-7bc7dfe238ad")
PRIVATE = "PRIVATE_TOMBSTONE_CONTENT_SQL_PASSWORD_SENTINEL"


class ScriptedSession:
    def __init__(self, *values):
        self.values = list(values)
        self.statements = []
        self.added = []
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        value = self.values.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    def add(self, row):
        self.added.append(row)


def test_ingestion_key_is_fixed_signed_64_bit_digest_of_uuid_bytes():
    expected = int.from_bytes(
        hashlib.sha256(b"kulai_memory.ingestion.v1\0" + IDENTITY.bytes).digest()[:8],
        "big", signed=True,
    )
    assert ingestion_lock_key(IDENTITY) == ingestion_lock_key(UUID(str(IDENTITY))) == expected
    assert -(2**63) <= expected < 2**63
    assert ingestion_lock_key(UUID(int=0)) != expected


@pytest.mark.parametrize("operation", ["create", "create_or_get_by_ingestion_id"])
def test_retired_ingestion_blocks_every_repository_insert_and_is_safe(operation):
    async def scenario():
        db = ScriptedSession(None, IDENTITY)
        repository = PostgresMemoryRepository(db=db)
        candidate = Memory(ingestion_id=IDENTITY, content=PRIVATE, metadata={"secret": PRIVATE})
        with pytest.raises(MemoryIngestionRetiredError) as caught:
            await getattr(repository, operation)(candidate)
        assert str(caught.value) == MemoryIngestionRetiredError.safe_message
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert db.added == [] and db.flush.await_count == 0
        sql = [str(statement.compile(dialect=postgresql.dialect())) for statement, _ in db.statements]
        assert "pg_advisory_xact_lock" in sql[0]
        assert "memory_ingestion_tombstones" in sql[1]
        assert all("INSERT" not in statement for statement in sql)
        db.commit.assert_not_called()
        db.rollback.assert_not_called()
    asyncio.run(scenario())


def test_neutral_memory_service_preserves_retired_error():
    async def scenario():
        class Repository:
            async def create_or_get_by_ingestion_id(self, memory):
                raise MemoryIngestionRetiredError()
        service = MemoryService(repository=Repository())
        with pytest.raises(MemoryIngestionRetiredError):
            await service.create_memory_idempotent(ingestion_id=IDENTITY, content=PRIVATE)
    asyncio.run(scenario())


def test_tombstone_orm_has_exact_technical_fields_and_no_foreign_keys():
    table = Base.metadata.tables[MemoryIngestionTombstoneDb.__tablename__]
    assert set(table.columns.keys()) == {"ingestion_id", "memory_id", "deleted_at"}
    assert table.c.ingestion_id.primary_key and not table.c.ingestion_id.nullable
    assert all(not column.nullable for column in table.columns)
    assert table.c.deleted_at.type.timezone
    assert str(table.c.deleted_at.server_default.arg) == "now()"
    assert not table.foreign_keys
    assert any(constraint.name == "uq_memory_ingestion_tombstones_memory_id"
               for constraint in table.constraints)


def test_repeat_delete_rechecks_under_ingestion_lock_without_tombstone_write():
    async def scenario():
        db = ScriptedSession(None, IDENTITY, None, None)
        repository = PostgresMemoryRepository(db=db)
        assert await repository.delete_by_id(UUID(int=42)) is False
        sql = [str(statement) for statement, _ in db.statements]
        assert "pg_advisory_xact_lock" in sql[2]
        assert "FOR UPDATE" in sql[3]
        assert all("INSERT" not in statement and "DELETE" not in statement for statement in sql)
        db.commit.assert_not_called()
        db.rollback.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("problem", ["none", "missing", "private_column", "foreign_key", "no_unique"])
def test_doctor_requires_exact_private_free_tombstone_schema(problem):
    async def scenario():
        columns = [
            SimpleNamespace(column_name="ingestion_id", udt_name="uuid", is_nullable="NO", column_default=None),
            SimpleNamespace(column_name="memory_id", udt_name="uuid", is_nullable="NO", column_default=None),
            SimpleNamespace(column_name="deleted_at", udt_name="timestamptz", is_nullable="NO", column_default="now()"),
        ]
        constraints = [("p", "PRIMARY KEY (ingestion_id)"), ("u", "UNIQUE (memory_id)")]
        if problem == "missing":
            columns, constraints = [], []
        elif problem == "private_column":
            columns.append(SimpleNamespace(column_name="content", udt_name="text", is_nullable="YES", column_default=None))
        elif problem == "foreign_key":
            constraints.append(("f", "FOREIGN KEY (memory_id) REFERENCES memories(id)"))
        elif problem == "no_unique":
            constraints.pop()
        connection = SimpleNamespace(
            scalar=AsyncMock(return_value=problem != "missing"),
            execute=AsyncMock(side_effect=[columns, constraints]),
        )
        checks = await _tombstone_schema_checks(connection)
        assert all(check.ok for check in checks) is (problem == "none")
    asyncio.run(scenario())
