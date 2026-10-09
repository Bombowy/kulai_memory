from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from scripts import db_backup
from kulai_memory.database_safety import memory_fingerprint


def row(**updates):
    value = dict(id=UUID(int=1), ingestion_id=UUID(int=2), content="synthetic content", source_kind="voice",
        session_id=None, metadata_json={}, created_at=datetime(2025,1,1,tzinfo=UTC), revision=1, archived_at=None)
    value.update(updates)
    return SimpleNamespace(_mapping=value)


class Connection:
    def __init__(self, rows, *, lifecycle=True):
        self.rows, self.lifecycle = rows, lifecycle
    async def scalar(self, statement): return self.lifecycle
    async def stream(self, statement):
        assert "ORDER BY id" in str(statement)
        assert ("archived_at" in str(statement)) == self.lifecycle
        rows = sorted(self.rows, key=lambda r:r._mapping["id"])
        class Rows:
            async def __aiter__(self):
                for item in rows: yield item
        return Rows()


def fingerprint(rows, **kwargs):
    return asyncio.run(memory_fingerprint(Connection(rows, **kwargs)))


def test_current_and_legacy_fingerprints_are_deterministic_and_order_independent():
    rows = [row(id=UUID(int=1)), row(id=UUID(int=3))]
    assert fingerprint(rows) == fingerprint(list(reversed(rows)))
    assert fingerprint(rows) == fingerprint(rows)
    legacy = fingerprint(rows, lifecycle=False)
    assert legacy != fingerprint(rows)
    assert legacy == fingerprint([row(id=UUID(int=1), revision=20), row(id=UUID(int=3))], lifecycle=False)


@pytest.mark.parametrize("update", [{"revision":2}, {"archived_at":datetime(2025,2,1,tzinfo=UTC)}])
def test_revision_and_archived_at_are_durable_state(update):
    first, second = fingerprint([row()]), fingerprint([row(**update)])
    assert first.count == second.count == 1 and first.sha256 != second.sha256


def test_archive_timestamp_canonicalizes_to_utc():
    instant = datetime(2025,2,1,tzinfo=UTC)
    assert fingerprint([row(archived_at=instant)]) == fingerprint([row(archived_at=instant.astimezone(timezone(timedelta(hours=2))))])


def test_empty_real_current_table_has_real_zero_fingerprint():
    result = fingerprint([])
    assert result.count == 0 and len(result.sha256) == 64
    assert "synthetic" not in repr(result)
