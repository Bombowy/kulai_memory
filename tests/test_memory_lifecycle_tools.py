from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from scripts import manage_memory as cli
from kulai_memory import database_safety as safety
from kulai_memory.application.memory import Memory
from kulai_memory.settings import Settings


def arguments(action="archive", *extra):
    return cli.parser().parse_args([action, "--memory-id", str(uuid4()), *extra])


def test_default_dry_run_and_explicit_execute_contract():
    args = arguments()
    assert not args.execute and not args.confirm_main_memory_write
    assert cli.parser().parse_args(["show", "--memory-id", str(uuid4())]).action == "show"
    with pytest.raises(SystemExit): arguments("archive", "--execute", "--dry-run")
    with pytest.raises(SystemExit): arguments("edit", "--expected-revision", "0", "--content-file", "note.txt")
    with pytest.raises(SystemExit): cli.main(["archive", "--memory-id", str(uuid4()), "--execute"])


@pytest.fixture
def environment(monkeypatch):
    memory = Memory(content="PRIVATE_CONTENT_SENTINEL")
    calls = []
    config = SimpleNamespace(async_url="postgresql+asyncpg://user:PRIVATE_PASSWORD@127.0.0.1/source")
    settings = Settings(_env_file=None, kulai_vector_dimension=1024)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "database_config", lambda: config)
    async def doctor(**kwargs): return SimpleNamespace(ok=True)
    monkeypatch.setattr(cli, "run_database_doctor", doctor)
    class Engine:
        async def dispose(self): calls.append("dispose")
    monkeypatch.setattr(cli, "create_async_engine", lambda *a: Engine())
    @asynccontextmanager
    async def session():
        class Session:
            async def execute(self, sql): assert str(sql) == "SET TRANSACTION READ ONLY"
            async def rollback(self): calls.append("rollback")
        yield Session()
    monkeypatch.setattr(cli, "async_sessionmaker", lambda *a, **k: session)
    async def get(memory_id): return memory
    monkeypatch.setattr(cli, "PostgresMemoryRepository", lambda **k: SimpleNamespace(get_by_id=get))
    def forbidden(*a, **k): pytest.fail("Dry run called a write/embedding/backup path")
    monkeypatch.setattr(cli, "AutomaticMemoryIndexer", forbidden)
    monkeypatch.setattr(cli, "_backup_and_verify", forbidden)
    monkeypatch.setattr(cli, "change_memory", forbidden)
    return memory, calls, config, settings


@pytest.mark.parametrize("action", ["archive", "restore", "edit"])
def test_dry_run_has_zero_embeddings_backups_and_writes(environment, tmp_path, action, capsys):
    path = tmp_path / "new.txt"
    path.write_text("PRIVATE_EDIT_CONTENT_SENTINEL", encoding="utf-8")
    extra = ["--expected-revision", "1", "--content-file", str(path)] if action == "edit" else []
    assert asyncio.run(cli.run(arguments(action, *extra))) == 0
    output = capsys.readouterr()
    assert "DRY_RUN_OK" in output.out and environment[1] == ["rollback", "dispose"]
    assert "PRIVATE_" not in output.out + output.err


@pytest.mark.parametrize("guard", ["confirmation", "backup", "environment", "remote", "doctor", "path"])
def test_execution_guard_rejects_before_any_write(environment, monkeypatch, tmp_path, guard, capsys):
    memory, calls, config, settings = environment
    args = arguments("archive", "--execute", "--confirm-main-memory-write", "--backup-output", str(tmp_path / "backup.dump"))
    if guard == "confirmation": args.confirm_main_memory_write = False
    if guard == "backup": args.backup_output = None
    if guard == "environment": settings.app_env = "production"
    if guard == "remote": config.async_url = "postgresql+asyncpg://user:PRIVATE_PASSWORD@remote.invalid/source"
    if guard == "doctor":
        async def doctor(**k): return SimpleNamespace(ok=False)
        monkeypatch.setattr(cli, "run_database_doctor", doctor)
    if guard == "path": args.backup_output = cli.PROJECT_ROOT / "forbidden.dump"
    assert asyncio.run(cli.run(args)) == 1
    output = capsys.readouterr()
    assert "PRIVATE_" not in output.out + output.err


@pytest.mark.parametrize("failed", ["backup", "restore"])
def test_backup_restore_failure_blocks_mutation(environment, monkeypatch, tmp_path, failed):
    calls = environment[1]
    async def snapshot(*a, **k): return "unchanged"
    async def failure(*a, **k):
        calls.append(failed)
        raise RuntimeError("PRIVATE_BACKUP_ERROR")
    monkeypatch.setattr(cli, "database_snapshot_url", snapshot)
    monkeypatch.setattr(cli, "_backup_and_verify", failure)
    args = arguments("archive", "--execute", "--confirm-main-memory-write", "--backup-output", str(tmp_path / "backup.dump"))
    assert asyncio.run(cli.run(args)) == 1
    assert calls == ["rollback", failed, "dispose"]


def report_four(source):
    failures = safety.pre_migration_failures(source, "kulai_memory_0004")
    names = safety._PRE_MIGRATION_REQUIRED_CHECKS | failures
    columns = {name: dict(type=value[0], nullable=value[1], max_length=value[2])
               for name, value in safety.LEGACY_MEMORY_COLUMNS.items()}
    columns.update({name: dict(type=None, nullable=None, max_length=None) for name in ("revision", "archived_at")})
    values = {"alembic.expected_head": "kulai_memory_0004", "alembic.current": [source],
        "schema.memories": columns, "constraint.memories_revision_positive": dict(exists=False, valid=False),
        "schema.memory_ingestion_tombstones": {"columns": []} if source.endswith("0002") else {"columns": ["deleted_at", "ingestion_id", "memory_id"]}}
    return safety.DoctorReport(tuple(safety.CheckResult(name, name not in failures, values.get(name)) for name in names))


@pytest.mark.parametrize("source", ["kulai_memory_0002", "kulai_memory_0003"])
def test_explicit_legacy_schema_profiles_before_four(monkeypatch, source):
    monkeypatch.setattr(safety, "expected_alembic_heads", lambda: ("kulai_memory_0004",))
    report = report_four(source)
    safety.validate_backup_doctor(report, pre_migration_from=source)
    with pytest.raises(safety.DatabaseSafetyError): safety.validate_backup_doctor(report)


@pytest.mark.parametrize("tamper", ["old-column", "partial-new-column", "constraint", "diagnostics", "extra-failure"])
def test_old_schema_exception_does_not_hide_unrelated_corruption(monkeypatch, tamper):
    monkeypatch.setattr(safety, "expected_alembic_heads", lambda: ("kulai_memory_0004",))
    report = report_four("kulai_memory_0003")
    checks = list(report.checks)
    if tamper in {"old-column", "partial-new-column"}:
        index = next(i for i,c in enumerate(checks) if c.name == "schema.memories")
        columns = dict(checks[index].value)
        columns["content" if tamper == "old-column" else "revision"] = dict(type="uuid", nullable=False, max_length=None)
        checks[index] = replace(checks[index], value=columns)
    elif tamper == "constraint":
        index = next(i for i,c in enumerate(checks) if c.name == "constraint.memories_revision_positive")
        checks[index] = replace(checks[index], value=dict(exists=True, valid=False))
    elif tamper == "diagnostics": checks.append(safety.CheckResult("database.diagnostics", False))
    else: checks.append(safety.CheckResult("unexpected", False))
    with pytest.raises(safety.DatabaseSafetyError):
        safety.validate_backup_doctor(safety.DoctorReport(tuple(checks)), pre_migration_from="kulai_memory_0003")
