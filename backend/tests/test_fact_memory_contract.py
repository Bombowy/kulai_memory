"""Portable boundary and fail-closed migration/authority checks, without a database."""

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from kulai_memory.application.fact_memory import FactMemoryError, FactMemoryService
from kulai_memory.fact_persistence.migration import INI, fact_migration_heads, upgrade_owned_facts
from kulai_memory.fact_persistence.owned import OwnedFactStorage, _LocalAuthority
from kulai_memory.database_safety import OwnedTemporaryDatabase, database_config
from kulai_memory.security_persistence.migration import security_migration_heads


def test_all_three_graphs_are_separate_and_keep_existing_heads():
    root = Path(__file__).resolve().parents[2]
    assert ScriptDirectory.from_config(Config(str(root / "backend/alembic.ini"))).get_heads() == ["kulai_memory_0004"]
    assert security_migration_heads() == ("kulai_security_0003",)
    assert fact_migration_heads() == ("kulai_facts_0001",)


def test_unscoped_alembic_invocation_fails_before_any_connection():
    with pytest.raises(RuntimeError, match="marker-owned"):
        command.upgrade(Config(str(INI)), "head")


def test_main_cannot_be_disguised_as_an_owned_database():
    config = database_config()
    from kulai_memory.database_safety import postgres_connection
    forged = OwnedTemporaryDatabase(postgres_connection(config).database, "kulai-memory-backup-test:" + "0" * 32, "backup")
    with pytest.raises(FactMemoryError, match="owned_database_required"):
        asyncio.run(upgrade_owned_facts(forged, config=config))
    with pytest.raises(ValueError):
        OwnedFactStorage(None, forged)


def test_authority_requires_the_exact_local_capability():
    authority = _LocalAuthority()
    assert authority.require(authority.principal).startswith("local-test:")
    for fake in ("admin", {"reviewer": "admin"}, _LocalAuthority().principal, None):
        with pytest.raises(FactMemoryError, match="forbidden"):
            authority.require(fake)


@pytest.mark.parametrize("limit", [0, 101, True, "50"])
def test_invalid_paging_performs_no_persistence_io(limit):
    service = FactMemoryService(storage=None, authority=None, intelligence=None)
    with pytest.raises(FactMemoryError, match="invalid_input"):
        asyncio.run(service.list_pending(limit=limit))


def test_core_and_sql_repository_preserve_architecture_boundaries():
    root = Path(__file__).resolve().parents[1] / "src/kulai_memory"
    for file in (root / "application/fact_memory").glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in {"sqlalchemy", "fastapi", "pathlib", "subprocess"}
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] not in {"sqlalchemy", "fastapi"} for a in node.names)
    tree = ast.parse((root / "fact_persistence/repository.py").read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {"commit", "rollback", "create_all"} for n in ast.walk(tree))


def test_no_startup_or_transport_imports_fact_storage():
    root = Path(__file__).resolve().parents[1] / "src/kulai_memory"
    files = [root / "main.py", root / "server/runtime.py", root / "application/ingestion.py"]
    for file in files:
        assert "fact_persistence" not in file.read_text(encoding="utf-8")
