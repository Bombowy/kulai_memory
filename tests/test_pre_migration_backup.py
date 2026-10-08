from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import db_backup, db_restore_smoke
from kulai_memory import database_safety as safety


SOURCE = "kulai_memory_0002"
HEAD = "kulai_memory_0003"
PRIVATE = "PRIVATE_MEMORY_VECTOR_METADATA_PASSWORD_SQL_SENTINEL"
CHECK_NAMES = (
    "alembic.expected_head", "database.connection", "database.transaction_read_only",
    "postgres.version", "alembic.current", "extension.vector", "table.memories",
    "table.kulai_vector_records", "schema.memories", "constraint.memories_ingestion_id_unique",
    "vector.embedding_dimension", "table.memory_ingestion_tombstones",
    "schema.memory_ingestion_tombstones", "constraint.memory_ingestion_tombstones",
    "database.read_query",
)


def report(*, current=SOURCE, strict=False):
    values = {
        "alembic.expected_head": HEAD, "alembic.current": [current],
        "postgres.version": {"display": "18.6", "number": "180006"},
        "schema.memory_ingestion_tombstones": {"columns": []},
    }
    return safety.DoctorReport(tuple(safety.CheckResult(
        name, strict or name not in safety.PRE_MIGRATION_FAILURES,
        values.get(name), message=PRIVATE,
    ) for name in CHECK_NAMES))


def changed(original, name, **kwargs):
    return safety.DoctorReport(tuple(
        replace(check, **kwargs) if check.name == name else check
        for check in original.checks
    ))


@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    monkeypatch.setattr(safety, "expected_alembic_heads", lambda: (HEAD,))
    config = SimpleNamespace(async_url=f"postgresql+asyncpg://user:{PRIVATE}@localhost/source")
    for module in (db_backup, db_restore_smoke):
        monkeypatch.setattr(module, "database_config", lambda: config)
        monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(app_env="test"))


def test_cli_default_remains_strict_and_new_option_is_explicit():
    assert db_backup.parser().parse_args(["--output", "example.dump"]).pre_migration_from is None
    assert db_restore_smoke.parser().parse_args(["example.dump"]).pre_migration_from is None
    assert db_backup.parser().parse_args([
        "--output", "example.dump", "--pre-migration-from", SOURCE,
    ]).pre_migration_from == SOURCE
    assert db_restore_smoke.parser().parse_args([
        "example.dump", "--pre-migration-from", SOURCE,
    ]).pre_migration_from == SOURCE
    for module in (db_backup, db_restore_smoke):
        actions = {action.dest for action in module.parser()._actions}
        assert not actions & {"database", "target", "ignore_errors", "force_doctor"}


def test_strict_default_rejects_pending_schema_but_accepts_full_pass():
    with pytest.raises(safety.DatabaseSafetyError, match="full doctor PASS"):
        safety.validate_backup_doctor(report())
    safety.validate_backup_doctor(report(current=HEAD, strict=True))


def test_exact_pre_migration_gap_accepted_without_changing_doctor_status():
    pending = report()
    safety.validate_backup_doctor(pending, pre_migration_from=SOURCE)
    assert not pending.ok


@pytest.mark.parametrize("heads", [(), (SOURCE, HEAD), ("unreviewed_head",)])
def test_missing_multiple_or_unknown_local_heads_rejected(monkeypatch, heads):
    monkeypatch.setattr(safety, "expected_alembic_heads", lambda: heads)
    with pytest.raises(safety.DatabaseSafetyError):
        safety.validate_backup_doctor(report(), pre_migration_from=SOURCE)


def test_source_at_head_rejected():
    with pytest.raises(safety.DatabaseSafetyError, match="already the local head"):
        safety.validate_backup_doctor(report(current=HEAD, strict=True), pre_migration_from=HEAD)


@pytest.mark.parametrize("revision", [HEAD, "kulai_memory_0001", None])
def test_database_revision_must_match_source(revision):
    bad = changed(report(), "alembic.current", value=[revision])
    with pytest.raises(safety.DatabaseSafetyError, match="requested pre-migration source"):
        safety.validate_backup_doctor(bad, pre_migration_from=SOURCE)


@pytest.mark.parametrize("check_name", sorted(set(CHECK_NAMES) - safety.PRE_MIGRATION_FAILURES))
def test_each_unrelated_failure_blocks_backup(tmp_path, monkeypatch, check_name):
    install_backup(monkeypatch, changed(report(), check_name, ok=False))
    with pytest.raises(safety.DatabaseSafetyError):
        asyncio.run(db_backup.create_backup(tmp_path / "blocked.dump", force=False, pre_migration_from=SOURCE))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("variant", ["diagnostics", "unknown", "missing", "duplicate", "partial-table"])
def test_incomplete_ambiguous_or_extra_failures_rejected(variant):
    pending = report()
    if variant in {"diagnostics", "unknown"}:
        name = "database.diagnostics" if variant == "diagnostics" else "unexpected.check"
        pending = safety.DoctorReport((*pending.checks, safety.CheckResult(name, False, message=PRIVATE)))
    elif variant == "missing":
        pending = safety.DoctorReport(pending.checks[:-1])
    elif variant == "duplicate":
        pending = safety.DoctorReport((*pending.checks, pending.checks[0]))
    else:
        pending = changed(pending, "schema.memory_ingestion_tombstones", value={"columns": ["content"]})
    with pytest.raises(safety.DatabaseSafetyError) as caught:
        safety.validate_backup_doctor(pending, pre_migration_from=SOURCE)
    assert PRIVATE not in str(caught.value)


def install_backup(monkeypatch, doctor_report):
    calls = []
    async def doctor(**kwargs):
        calls.append("doctor")
        return doctor_report
    def dump(executable, arguments, *, connection):
        calls.append("dump")
        assert connection.password == PRIVATE
        assert PRIVATE not in " ".join(arguments)
        assert {"--format=custom", "--no-owner", "--no-privileges"}.issubset(arguments)
        Path(arguments[arguments.index("--file") + 1]).write_bytes(b"PGDMP synthetic archive")
        return subprocess.CompletedProcess(arguments, 0, PRIVATE, PRIVATE)
    monkeypatch.setattr(db_backup, "run_database_doctor", doctor)
    monkeypatch.setattr(db_backup, "find_postgres_tool", lambda name: Path(name))
    monkeypatch.setattr(db_backup, "postgres_tool_version", lambda path: "18.6")
    monkeypatch.setattr(db_backup, "run_postgres_tool", dump)
    return calls


@pytest.mark.parametrize("mode", [None, SOURCE])
def test_backup_doctor_gate_and_atomic_success(tmp_path, monkeypatch, mode):
    pending = report()
    calls = install_backup(monkeypatch, pending)
    output = tmp_path / "archive.dump"
    if mode is None:
        with pytest.raises(safety.DatabaseSafetyError):
            asyncio.run(db_backup.create_backup(output, force=False))
        assert calls == ["doctor"] and not output.exists()
    else:
        size, digest, version = asyncio.run(db_backup.create_backup(
            output, force=False, pre_migration_from=mode,
        ))
        assert size > 0 and len(digest) == 64 and version == "18.6"
        assert calls == ["doctor", "dump", "doctor"]
        assert list(tmp_path.iterdir()) == [output]


def test_source_revision_change_during_dump_does_not_publish_archive(tmp_path, monkeypatch):
    calls = install_backup(monkeypatch, report())
    reports = iter((report(), report(current=HEAD, strict=True)))
    async def doctor(**kwargs):
        return next(reports)
    monkeypatch.setattr(db_backup, "run_database_doctor", doctor)
    with pytest.raises(safety.DatabaseSafetyError):
        asyncio.run(db_backup.create_backup(tmp_path / "archive.dump", force=False, pre_migration_from=SOURCE))
    assert calls == ["dump"] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("module", [db_backup, db_restore_smoke])
@pytest.mark.parametrize("mode", [None, SOURCE])
@pytest.mark.parametrize("url,env", [
    ("postgresql+asyncpg://u:p@remote.example/source", "test"),
    ("postgresql+asyncpg://u:p@localhost/source", "production"),
    *[(f"postgresql+asyncpg://u:p@localhost/{name}", "dev") for name in (
        "postgres", "template0", "template1",
        safety.BACKUP_TEST_DATABASE_PREFIX + "a" * 32,
        safety.RESTORE_DATABASE_PREFIX + "b" * 32,
    )],
])
def test_public_target_guards_before_doctor(tmp_path, monkeypatch, module, mode, url, env):
    monkeypatch.setattr(module, "database_config", lambda: SimpleNamespace(async_url=url))
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(app_env=env))
    async def unexpected(**kwargs):
        pytest.fail("Unsafe target must be rejected before doctor or DB work")
    monkeypatch.setattr(module, "run_database_doctor", unexpected)
    with pytest.raises(ValueError):
        if module is db_backup:
            asyncio.run(module.create_backup(tmp_path / "archive.dump", force=False, pre_migration_from=mode))
        else:
            asyncio.run(module.restore_smoke(tmp_path / "archive.dump", pre_migration_from=mode))


def test_output_safety_and_explicit_force(tmp_path):
    inside = safety.PROJECT_ROOT / "must-not-be-created.dump"
    with pytest.raises(ValueError, match="outside"):
        safety.validate_backup_output(inside, force=True)
    output = tmp_path / "existing.dump"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        safety.validate_backup_output(output, force=False)
    assert safety.validate_backup_output(output, force=True) == output.resolve()
    assert output.read_bytes() == b"existing"
    with pytest.raises(ValueError, match="regular"):
        safety.validate_backup_output(tmp_path, force=True)
    with pytest.raises(FileNotFoundError):
        safety.validate_backup_output(tmp_path / "missing" / "archive.dump", force=False)


def test_output_symlink_and_parent_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable to this process")
    with pytest.raises(ValueError, match="symbolic link"):
        safety.validate_backup_output(link / "archive.dump", force=False)


@pytest.mark.parametrize("method", ["is_symlink", "is_junction"])
def test_link_component_guard_without_os_link_permission(tmp_path, monkeypatch, method):
    output = tmp_path / "archive.dump"
    monkeypatch.setattr(Path, method, lambda path: path == tmp_path, raising=False)
    with pytest.raises(ValueError, match="symbolic link or junction"):
        safety.validate_backup_output(output, force=False)


def install_restore(monkeypatch, tmp_path, *, reports=None, fingerprints=None, restore_error=None):
    calls = []
    archive = tmp_path / "archive.dump"
    archive.write_bytes(b"PGDMP synthetic archive")
    owned = safety.OwnedTemporaryDatabase(
        safety.RESTORE_DATABASE_PREFIX + "a" * 32,
        "kulai-memory-restore:" + "b" * 32, "restore",
    )
    before = (safety.MemoryFingerprint(2, "a" * 64), safety.VectorFingerprint(1, "b" * 64))
    doctor_reports = iter(reports if reports is not None else [report(), report(), report()])
    fingerprint_results = iter(fingerprints if fingerprints is not None else [before, before, before])
    async def doctor(**kwargs):
        calls.append("doctor")
        return next(doctor_reports)
    async def fingerprint(url):
        calls.append("fingerprint")
        return next(fingerprint_results)
    async def create(**kwargs):
        calls.append("create")
        return owned
    async def restore(*args, **kwargs):
        calls.append("restore")
        if restore_error:
            raise restore_error
    async def drop(*args, **kwargs):
        calls.append("drop")
    monkeypatch.setattr(db_restore_smoke, "run_database_doctor", doctor)
    monkeypatch.setattr(db_restore_smoke, "fingerprint_url", fingerprint)
    monkeypatch.setattr(db_restore_smoke, "create_owned_temporary_database", create)
    monkeypatch.setattr(db_restore_smoke, "restore_archive_to_owned_database", restore)
    monkeypatch.setattr(db_restore_smoke, "drop_owned_temporary_database", drop)
    return archive, owned, before, calls


def test_restore_pre_migration_success_retains_archive_and_cleans_db(tmp_path, monkeypatch):
    archive, owned, before, calls = install_restore(monkeypatch, tmp_path)
    assert asyncio.run(db_restore_smoke.restore_smoke(archive, pre_migration_from=SOURCE)) == (owned.name, 2, 1)
    assert calls == ["doctor", "fingerprint", "create", "restore", "doctor", "fingerprint", "fingerprint", "doctor", "drop"]
    assert archive.is_file()


def test_strict_restore_requires_full_pass(tmp_path, monkeypatch):
    archive, _, _, calls = install_restore(monkeypatch, tmp_path, reports=[report()])
    with pytest.raises(safety.DatabaseSafetyError, match="full doctor PASS"):
        asyncio.run(db_restore_smoke.restore_smoke(archive))
    assert calls[-1] == "drop"


def test_strict_restore_success(tmp_path, monkeypatch):
    archive, owned, _, calls = install_restore(monkeypatch, tmp_path, reports=[report(current=HEAD, strict=True)])
    assert asyncio.run(db_restore_smoke.restore_smoke(archive)) == (owned.name, 2, 1)
    assert calls[-1] == "drop"


@pytest.mark.parametrize("failure", ["source-revision", "restored-revision", "extra-check", "fingerprint", "source-mutation", "source-revision-change"])
def test_restore_rejects_mismatch_and_always_cleans_owned_target(tmp_path, monkeypatch, failure):
    baseline = (safety.MemoryFingerprint(2, "a" * 64), safety.VectorFingerprint(1, "b" * 64))
    modified = (baseline[0], safety.VectorFingerprint(1, "c" * 64))
    reports = [report(), report(), report()]
    fingerprints = [baseline, baseline, baseline]
    if failure == "source-revision":
        reports[0] = report(current=HEAD, strict=True)
    elif failure == "restored-revision":
        reports[1] = report(current=HEAD, strict=True)
    elif failure == "extra-check":
        reports[1] = changed(report(), "extension.vector", ok=False)
    elif failure == "fingerprint":
        fingerprints[1] = modified
    elif failure == "source-mutation":
        fingerprints[2] = modified
    else:
        reports[2] = report(current=HEAD, strict=True)
    archive, _, _, calls = install_restore(monkeypatch, tmp_path, reports=reports, fingerprints=fingerprints)
    with pytest.raises(safety.DatabaseSafetyError) as caught:
        asyncio.run(db_restore_smoke.restore_smoke(archive, pre_migration_from=SOURCE))
    assert PRIVATE not in str(caught.value)
    if failure == "source-revision":
        assert calls == ["doctor"]
    else:
        assert calls[-1] == "drop"


@pytest.mark.parametrize("error", [RuntimeError(PRIVATE), asyncio.CancelledError()])
def test_restore_failure_and_cancellation_cleanup(tmp_path, monkeypatch, error):
    archive, _, _, calls = install_restore(monkeypatch, tmp_path, restore_error=error)
    with pytest.raises(type(error)):
        asyncio.run(db_restore_smoke.restore_smoke(archive, pre_migration_from=SOURCE))
    assert calls[-1] == "drop"


def test_cleanup_failure_cannot_report_success(tmp_path, monkeypatch):
    archive, _, _, _ = install_restore(monkeypatch, tmp_path)
    async def drop(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(db_restore_smoke, "drop_owned_temporary_database", drop)
    with pytest.raises(safety.DatabaseSafetyError, match="Safe cleanup failed") as caught:
        asyncio.run(db_restore_smoke.restore_smoke(archive, pre_migration_from=SOURCE))
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("module,arguments", [
    (db_backup, ["--output", "archive.dump", "--pre-migration-from", SOURCE]),
    (db_restore_smoke, ["archive.dump", "--pre-migration-from", SOURCE]),
])
def test_cli_never_echoes_raw_private_errors(monkeypatch, capsys, module, arguments):
    async def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(module, "run", fail)
    assert module.main(arguments) == 1
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err
    assert "failed safely" in output.err
