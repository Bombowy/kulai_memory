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
    assert ScriptDirectory.from_config(Config(str(backend / "alembic.ini"))).get_heads() == ["kulai_memory_0004"]
    assert security_migration_heads() == ("kulai_security_0001",)
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
