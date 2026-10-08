from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import db_restore_smoke  # Establish host source paths.
from kulai_memory import database_safety as safety


PRIVATE = "PRIVATE_CONTENT_METADATA_PASSWORD_VECTOR_SENTINEL"
DATE = datetime(2025, 1, 2, 3, 4, 5, 6789, tzinfo=UTC)


def row(index=1, **changes):
    data = dict(ingestion_id=UUID(int=index), memory_id=UUID(int=index + 1), deleted_at=DATE)
    data.update(changes)
    return SimpleNamespace(_mapping=data)


class Rows:
    def __init__(self, rows):
        self.rows = rows
    async def __aiter__(self):
        for item in self.rows:
            yield item


class Connection:
    def __init__(self, rows=(), *, revision="kulai_memory_0003", present=True):
        self.rows, self.revision, self.present = rows, revision, present
        self.sql = []
        self.rollbacks = 0
        self.closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True
    async def stream(self, statement):
        self.sql.append(str(statement))
        assert "ORDER BY ingestion_id" in str(statement)
        return Rows(sorted(self.rows, key=lambda item: item._mapping["ingestion_id"]))
    async def execute(self, statement):
        self.sql.append(str(statement))
        return SimpleNamespace(scalars=lambda: (self.revision,))
    async def scalar(self, statement):
        self.sql.append(str(statement))
        return self.present
    async def rollback(self):
        self.rollbacks += 1


def fingerprint(rows):
    return asyncio.run(safety.tombstone_fingerprint(Connection(rows)))


def test_known_canonical_fingerprint_and_public_shape():
    item = row()
    result = fingerprint([item])
    expected_bytes = (
        b'{"deleted_at":"2025-01-02T03:04:05.006789+00:00",'
        b'"ingestion_id":"00000000-0000-0000-0000-000000000001",'
        b'"memory_id":"00000000-0000-0000-0000-000000000002"}'
    )
    expected = hashlib.sha256(len(expected_bytes).to_bytes(8, "big") + expected_bytes).hexdigest()
    assert result == safety.TombstoneFingerprint(1, expected)
    assert result == fingerprint([item])
    assert str(item._mapping["ingestion_id"]) not in repr(result)
    assert str(item._mapping["memory_id"]) not in repr(result)
    assert not hasattr(result, "__dict__")


def test_database_order_is_requested_and_insertion_order_independent():
    original = [row(1), row(3), row(5)]
    assert fingerprint(original) == fingerprint(list(reversed(original)))


@pytest.mark.parametrize("changes", [
    {"ingestion_id": UUID(int=10)}, {"memory_id": UUID(int=20)},
    {"deleted_at": DATE + timedelta(microseconds=1)},
])
def test_each_technical_field_changes_hash_without_changing_count(changes):
    before, after = fingerprint([row()]), fingerprint([row(**changes)])
    assert before.count == after.count == 1
    assert before.sha256 != after.sha256


def test_equivalent_timestamp_offsets_have_same_utc_hash():
    offset = DATE.astimezone(timezone(timedelta(hours=2)))
    assert fingerprint([row(deleted_at=offset)]) == fingerprint([row()])


def test_naive_timestamp_rejected_without_exposing_row():
    with pytest.raises(safety.DatabaseSafetyError) as caught:
        fingerprint([row(deleted_at=DATE.replace(tzinfo=None))])
    assert str(UUID(int=1)) not in str(caught.value)
    assert "2025" not in str(caught.value)


def test_empty_real_table_fingerprint_is_distinct_from_absence():
    empty = fingerprint([])
    assert empty == safety.TombstoneFingerprint(0, hashlib.sha256(b"").hexdigest())
    before = safety.DatabaseSnapshot(
        ("kulai_memory_0002",), safety.MemoryFingerprint(0, "a" * 64),
        safety.VectorFingerprint(0, "b" * 64), None,
    )
    after = replace(before, revision=("kulai_memory_0003",), tombstones=empty)
    assert before != after and before.tombstones is None and after.tombstones.count == 0
    with pytest.raises(safety.DatabaseSafetyError):
        replace(after, tombstones=None)


def test_nontechnical_extra_data_does_not_enter_hash_or_public_result():
    item = row(content=PRIVATE, metadata=PRIVATE, vector=PRIVATE, password=PRIVATE)
    assert fingerprint([item]) == fingerprint([row()])
    assert PRIVATE not in repr(fingerprint([item]))


def install_engine(monkeypatch, connection):
    class Engine:
        disposed = False
        def connect(self):
            return connection
        async def dispose(self):
            self.disposed = True
    engine = Engine()
    monkeypatch.setattr(safety, "create_async_engine", lambda url: engine)
    monkeypatch.setattr(safety, "expected_alembic_heads", lambda: ("kulai_memory_0003",))
    async def memory(conn):
        assert conn is connection
        return safety.MemoryFingerprint(0, "a" * 64)
    async def vector(conn):
        assert conn is connection
        return safety.VectorFingerprint(0, "b" * 64)
    monkeypatch.setattr(safety, "memory_fingerprint", memory)
    monkeypatch.setattr(safety, "vector_fingerprint", vector)
    return engine


@pytest.mark.parametrize("rows", [[], [row()]])
def test_strict_snapshot_reads_real_zero_or_nonzero_tombstones_and_closes(monkeypatch, rows):
    connection = Connection(rows)
    engine = install_engine(monkeypatch, connection)
    result = asyncio.run(safety.database_snapshot_url("synthetic-url"))
    assert result.tombstones == fingerprint(rows)
    assert connection.sql[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert connection.closed and connection.rollbacks == 1 and engine.disposed
    assert all(not any(word in sql for word in ("INSERT ", "DELETE ", "UPDATE ", "CREATE ")) for sql in connection.sql)


def test_exact_pre_migration_snapshot_represents_absence(monkeypatch):
    connection = Connection(revision="kulai_memory_0002", present=False)
    engine = install_engine(monkeypatch, connection)
    result = asyncio.run(safety.database_snapshot_url("synthetic-url", pre_migration_from="kulai_memory_0002"))
    assert result.tombstones is None and result.revision == ("kulai_memory_0002",)
    assert not any("SELECT ingestion_id" in sql for sql in connection.sql)
    assert connection.closed and engine.disposed


@pytest.mark.parametrize("revision,present,mode", [
    ("kulai_memory_0003", False, None), ("kulai_memory_0002", False, None),
    ("kulai_memory_0003", False, "kulai_memory_0002"),
    ("kulai_memory_0002", True, "kulai_memory_0002"),
    ("kulai_memory_0002", False, "arbitrary-revision"),
])
def test_missing_table_is_not_a_general_snapshot_bypass(monkeypatch, revision, present, mode):
    connection = Connection(revision=revision, present=present)
    engine = install_engine(monkeypatch, connection)
    with pytest.raises(safety.DatabaseSafetyError):
        asyncio.run(safety.database_snapshot_url("synthetic-url", pre_migration_from=mode))
    assert connection.closed and connection.rollbacks == 1 and engine.disposed


def test_snapshot_failure_is_safe_and_disposes(monkeypatch):
    connection = Connection([row()])
    engine = install_engine(monkeypatch, connection)
    async def fail(statement):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(connection, "stream", fail)
    with pytest.raises(safety.DatabaseSafetyError) as caught:
        asyncio.run(safety.database_snapshot_url("synthetic-url"))
    assert PRIVATE not in str(caught.value)
    assert connection.closed and engine.disposed
