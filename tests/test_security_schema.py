"""Independent graph and explicit administrative CLI; never execute main migrations."""
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pathlib import Path

from scripts import security_schema
from kulai_memory.database_safety import DatabaseSnapshot, MemoryFingerprint, VectorFingerprint, TombstoneFingerprint
from kulai_memory.security_persistence.migration import security_migration_heads


def test_security_graph_preserves_canonical_head_and_legacy_snapshots():
    backend = Path(__file__).resolve().parents[1] / "backend"
    assert ScriptDirectory.from_config(Config(str(backend / "alembic.ini"))).get_heads() == ["kulai_memory_0005"]
    assert security_migration_heads() == ("kulai_security_0003",)
    snapshot = DatabaseSnapshot(("kulai_memory_0004",), MemoryFingerprint(0, "a" * 64),
                                VectorFingerprint(0, "b" * 64), TombstoneFingerprint(0, "c" * 64))
    assert snapshot.security is None


@pytest.mark.parametrize("arguments", [["upgrade"], ["upgrade", "--confirm-main-security-schema"],
    ["upgrade", "--backup-output", "outside.dump"]])
def test_upgrade_cli_requires_both_explicit_confirmation_and_backup(arguments, monkeypatch):
    def forbidden():
        pytest.fail("Unconfirmed migration accessed configuration/database.")
    monkeypatch.setattr(security_schema, "database_config", forbidden)
    with pytest.raises(SystemExit) as caught:
        security_schema.main(arguments)
    assert caught.value.code == 2


def test_historical_rate_fingerprint_encoding_stays_compatible():
    import asyncio
    import hashlib
    from datetime import datetime, timezone
    from kulai_memory.security_persistence.integrity import _fingerprint
    from kulai_memory.security_persistence.schema_v2 import RATE_TABLE
    class Rows:
        closed = False
        def mappings(self):
            return self
        async def partitions(self, size):
            assert size == 256
            yield [{'id':1, 'attempts':1, 'window_start':datetime(2026,1,1,tzinfo=timezone.utc)}]
        async def close(self):
            self.closed = True
    rows = Rows()
    class Connection:
        async def stream(self, statement, *, execution_options):
            assert execution_options == {'yield_per': 256}
            return rows
    # Golden historical payload: v2 encoded the integer primary key as a string.
    payload=b'{"attempts":1,"id":"1","window_start":"2026-01-01T00:00:00+00:00"}'
    result=asyncio.run(_fingerprint(Connection(),RATE_TABLE))
    assert result.count==1 and result.sha256==hashlib.sha256(len(payload).to_bytes(8,'big')+payload).hexdigest()
    assert rows.closed
