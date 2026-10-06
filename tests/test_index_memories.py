from __future__ import annotations

import asyncio
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector

from kulai_memory import backfill
from kulai_memory.application import MEMORY_VECTOR_NAMESPACE, Memory
from kulai_memory.database_safety import CheckResult, DoctorReport, MemoryFingerprint, VectorFingerprint
from scripts import index_memories as cli

PRIVATE = "PRIVATE_CONTENT_VECTOR_PASSWORD_SENTINEL"


class Harness:
    def __init__(self):
        self.memories = tuple(Memory(id=UUID(int=index), content=f"{PRIVATE}:{index}")
                              for index in range(1, 4))
        self.rows = {}
        self.calls = {name: 0 for name in ("provider", "embed", "close", "backup", "restore", "write", "dispose")}
        self.errors = set()
        self.memory_changed = False
        self.open_reads = 0
        self.doctor = DoctorReport(checks=(CheckResult(
            "vector.embedding_dimension", True,
            {"configured_dimension": 1024, "actual_dimension": 1024},
        ),))
        self.settings = SimpleNamespace(
            app_env="dev", kulai_embedding_model="bge-m3:567m-fp16",
            kulai_vector_dimension=1024,
        )
        self.config = SimpleNamespace(async_url=f"postgresql+asyncpg://u:{PRIVATE}@localhost/kulai_memory")

    def install(self, monkeypatch):
        harness = self

        class Reader:
            factory = object()

            def __init__(self, factory):
                pass

            async def fingerprints(self):
                digest = "changed" if harness.memory_changed else "canonical"
                return (MemoryFingerprint(3, digest), VectorFingerprint(len(harness.rows), str(harness.rows.keys())))

            async def select(self, *, limit, memory_id, reindex):
                eligible = [m.id for m in harness.memories if memory_id is None or m.id == memory_id]
                existing = [i for i in eligible if str(i) in harness.rows]
                selected = [i for i in eligible if reindex or i not in existing]
                return backfill.Selection(
                    tuple(selected[:limit]), await self.fingerprints(),
                    0 if reindex else len(existing),
                    sum(harness.rows[str(i)].metadata["embedding_model_tag"] != "bge-m3:567m-fp16"
                        for i in existing),
                )

            async def has_vector(self, memory_id):
                return str(memory_id) in harness.rows

            async def read_memory(self, memory_id, *, expected_fingerprint):
                assert (await self.fingerprints())[0] == expected_fingerprint
                harness.open_reads += 1
                try:
                    return next((m for m in harness.memories if m.id == memory_id), None)
                finally:
                    harness.open_reads -= 1

        class Provider:
            provider_id = "ollama"
            capabilities = EmbeddingCapabilities()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                harness.calls["close"] += 1

            async def embed(self, request):
                assert harness.open_reads == 0
                assert request.model_hint is None and request.purpose is None
                harness.calls["embed"] += 1
                if "preflight" in harness.errors:
                    raise RuntimeError(PRIVATE)
                return EmbeddingResponse(
                    provider_id="ollama", model_id="bge-m3:567m-fp16", dimension=1024,
                    embeddings=(EmbeddingVector(values=(1.0,) * 1024),),
                )

        def create_provider(*, settings):
            harness.calls["provider"] += 1
            return Provider()

        async def backup(output, *, force):
            assert force is False
            harness.calls["backup"] += 1
            if "backup" in harness.errors:
                raise RuntimeError(PRIVATE)
            output.write_bytes(b"synthetic backup")
            return output.stat().st_size, hashlib.sha256(output.read_bytes()).hexdigest(), "18.6"

        async def restore(output):
            harness.calls["restore"] += 1
            if "restore" in harness.errors:
                raise RuntimeError(PRIVATE)
            if "source_changed" in harness.errors:
                harness.memory_changed = True
            return "owned-restored-fixture", 3, 0

        async def save(*, service, request, session_factory):
            harness.calls["write"] += 1
            if "second_write" in harness.errors and harness.calls["write"] == 2:
                raise RuntimeError(PRIVATE)
            if "first_write" in harness.errors:
                raise RuntimeError(PRIVATE)
            record = request.records[0]
            harness.rows[record.id] = record
            if "changed_after_write" in harness.errors:
                harness.memory_changed = True

        async def dispose():
            harness.calls["dispose"] += 1

        async def doctor(**kwargs):
            return harness.doctor

        monkeypatch.setattr(cli, "get_settings", lambda: harness.settings)
        monkeypatch.setattr(cli, "database_config", lambda: harness.config)
        monkeypatch.setattr(cli, "run_database_doctor", doctor)
        monkeypatch.setattr(cli, "create_async_engine", lambda *a: SimpleNamespace(dispose=dispose))
        monkeypatch.setattr(cli, "async_sessionmaker", lambda *a, **k: object())
        monkeypatch.setattr(cli, "BackfillReader", Reader)
        monkeypatch.setattr(cli, "create_embedding_provider", create_provider)
        monkeypatch.setattr(cli.db_backup, "create_backup", backup)
        monkeypatch.setattr(cli.db_restore_smoke, "restore_smoke", restore)
        monkeypatch.setattr(backfill, "save_prepared_memory_vector", save)


@pytest.fixture
def harness(monkeypatch):
    result = Harness()
    result.install(monkeypatch)
    return result


def _execute(output):
    return ["--execute", "--confirm-main-vector-write", "--backup-output", str(output)]


def test_default_is_read_only_dry_run(harness, capsys):
    args = cli.parser().parse_args([])
    assert args.dry_run is True and args.execute is False
    assert args.missing_only is True and args.reindex is False
    assert args.limit == 100
    assert cli.main([]) == 0
    assert all(harness.calls[name] == 0 for name in ("provider", "embed", "backup", "restore", "write"))
    assert harness.rows == {}
    output = capsys.readouterr()
    assert "mode=dry-run" in output.out and "selected_count=3" in output.out
    assert "status=DRY_RUN_OK" in output.out
    assert PRIVATE not in output.out + output.err


@pytest.mark.parametrize("args", [
    ["--limit", "0"], ["--limit", "1001"], ["--limit", PRIVATE],
    ["--memory-id", PRIVATE], ["--missing-only", "--reindex"],
    ["--dry-run", "--execute"], ["--execute"],
    ["--execute", "--confirm-main-vector-write"],
    ["--execute", "--backup-output", PRIVATE], ["--unknown", PRIVATE],
])
def test_invalid_cli_is_safe_and_has_no_operations(args, harness, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2
    assert PRIVATE not in capsys.readouterr().err
    assert harness.calls["provider"] == harness.calls["backup"] == harness.calls["write"] == 0


@pytest.mark.parametrize("limit", [1, 1000])
def test_valid_limit_boundaries(limit):
    assert cli.parser().parse_args(["--limit", str(limit)]).limit == limit


@pytest.mark.parametrize("bad_setting", ["production", "test_environment", "remote", "dimension", "model", "schema", "doctor"])
def test_guards_block_provider_backup_and_writes(bad_setting, harness, tmp_path, capsys):
    if bad_setting == "production":
        harness.settings.app_env = "production"
    elif bad_setting == "test_environment":
        harness.settings.app_env = "test"
    elif bad_setting == "remote":
        harness.config.async_url = f"postgresql+asyncpg://u:{PRIVATE}@192.0.2.1/db"
    elif bad_setting == "dimension":
        harness.settings.kulai_vector_dimension = 768
    elif bad_setting == "model":
        harness.settings.kulai_embedding_model = PRIVATE
    elif bad_setting == "schema":
        harness.doctor = DoctorReport((CheckResult("vector.embedding_dimension", True,
                                      {"actual_dimension": 768, "configured_dimension": 1024}),))
    else:
        harness.doctor = DoctorReport((CheckResult("database.connection", False, message=PRIVATE),))
    assert cli.main(_execute(tmp_path / "guard.dump")) != 0
    assert harness.calls["provider"] == harness.calls["backup"] == harness.calls["write"] == 0
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err


def test_backup_inside_repo_is_rejected_without_operations(harness, capsys):
    assert cli.main(_execute(cli.PROJECT_ROOT / "forbidden.dump")) != 0
    assert harness.calls["backup"] == harness.calls["write"] == harness.calls["provider"] == 0
    assert not (cli.PROJECT_ROOT / "forbidden.dump").exists()


def test_backup_existing_file_is_never_overwritten(harness, tmp_path):
    output = tmp_path / "existing.dump"
    output.write_bytes(b"original")
    assert cli.main(_execute(output)) != 0
    assert output.read_bytes() == b"original"
    assert harness.calls["backup"] == harness.calls["write"] == 0


def test_resolved_backup_parent_cannot_point_into_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "resolve", lambda self: cli.PROJECT_ROOT / "alias.dump")
    with pytest.raises(backfill.BackfillError):
        cli.validate_backup_path(tmp_path / "alias.dump")


@pytest.mark.parametrize("stage", ["preflight", "backup", "restore", "source_changed"])
def test_preflight_backup_restore_failure_means_zero_writes(stage, harness, tmp_path, capsys):
    harness.errors.add(stage)
    output = tmp_path / "failure.dump"
    assert cli.main(_execute(output)) == 1
    assert harness.calls["write"] == 0
    assert harness.calls["provider"] == harness.calls["close"] == 1
    if stage == "preflight":
        assert harness.calls["backup"] == harness.calls["restore"] == 0
    if stage in {"restore", "source_changed"}:
        assert output.exists()  # User backup survives failed restore/verification.
    result = capsys.readouterr()
    assert PRIVATE not in result.out + result.err


def test_one_provider_batch_commit_and_retained_backup(harness, tmp_path, capsys):
    output = tmp_path / "success.dump"
    assert cli.main(_execute(output)) == 0
    assert len(harness.rows) == 3
    assert harness.calls == {"provider": 1, "embed": 4, "close": 1,
                             "backup": 1, "restore": 1, "write": 3, "dispose": 1}
    assert output.exists()
    result = capsys.readouterr()
    assert "indexed=3" in result.out and "memory_fingerprint_unchanged=true" in result.out
    assert PRIVATE not in result.out + result.err


def test_missing_only_resume_and_explicit_reindex(harness, tmp_path, capsys):
    harness.errors.add("second_write")
    assert cli.main(_execute(tmp_path / "partial.dump")) == 1
    assert len(harness.rows) == 1
    first = capsys.readouterr()
    assert "indexed=1" in first.out and "failed=1" in first.out
    assert "failed_memory_id=" in first.out
    harness.errors.clear()
    assert cli.main(_execute(tmp_path / "resume.dump")) == 0
    assert len(harness.rows) == 3
    second = capsys.readouterr()
    assert "selected_count=2" in second.out and "indexed=2" in second.out
    assert cli.main([]) == 0
    assert "selected_count=0" in capsys.readouterr().out
    assert cli.main(_execute(tmp_path / "reindex.dump") + ["--reindex"]) == 0
    assert len(harness.rows) == 3
    assert "selected_count=3" in capsys.readouterr().out


def test_changed_memory_after_commit_stops_batch_and_warns(harness, tmp_path, capsys):
    harness.errors.add("changed_after_write")
    assert cli.main(_execute(tmp_path / "changed.dump")) == 1
    assert len(harness.rows) == 1
    result = capsys.readouterr()
    assert "indexed=1" in result.out
    assert "memory_fingerprint_unchanged=false" in result.out
    assert "warning=memory_changed" in result.out


def test_memory_id_filter_and_limit_only_print_identifiers(harness, capsys):
    assert cli.main(["--memory-id", str(harness.memories[1].id), "--limit", "1"]) == 0
    result = capsys.readouterr()
    assert f"selected_memory_ids={harness.memories[1].id}" in result.out
    assert PRIVATE not in result.out


def test_reader_sql_is_read_only_ordered_and_namespace_scoped():
    source = inspect.getsource(backfill.BackfillReader)
    assert "REPEATABLE READ, READ ONLY" in source
    assert "ORDER BY m.created_at ASC, m.id ASC" in source
    assert "NOT EXISTS" in source
    assert "v.namespace_key = :namespace AND v.record_id = CAST(m.id AS text)" in source
    assert all(value not in source for value in ("session.commit(", "INSERT ", "UPDATE ", "DELETE "))


def test_owned_entry_requires_capability_before_any_target_operation(harness, monkeypatch, tmp_path):
    async def reject(*args, **kwargs):
        raise RuntimeError("ownership mismatch")
    monkeypatch.setattr(cli, "require_owned_database", reject)
    args = cli.parser().parse_args(["--execute", "--backup-output", str(tmp_path / "test.dump")])
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        asyncio.run(cli.run_owned(args, owned=object(), config=harness.config))
    assert harness.calls["provider"] == harness.calls["write"] == 0


def test_restore_owned_api_verifies_marker_before_restore(monkeypatch, tmp_path):
    calls = []
    from scripts import db_restore_smoke
    async def require(owned, *, config):
        calls.append("ownership")
    async def restore(backup, *, config):
        assert calls == ["ownership"]
        calls.append("restore")
        return "owned", 3, 0
    monkeypatch.setattr(db_restore_smoke, "validate_restore_source", lambda *a, **k: None)
    monkeypatch.setattr(db_restore_smoke, "require_owned_database", require)
    monkeypatch.setattr(db_restore_smoke, "database_config_for_database", lambda *a, **k: object())
    monkeypatch.setattr(db_restore_smoke, "_restore_verified_source", restore)
    result = asyncio.run(db_restore_smoke.restore_owned_database_backup(
        tmp_path / "test.dump", owned=SimpleNamespace(name="owned"), config=object()
    ))
    assert result == ("owned", 3, 0)
    assert calls == ["ownership", "restore"]


@pytest.mark.parametrize("corruption", ["empty", "size", "sha256"])
def test_unverified_backup_artifact_blocks_restore_and_all_writes(
    corruption, harness, monkeypatch, tmp_path, capsys
):
    async def corrupt(output, *, force):
        data = b"" if corruption == "empty" else b"synthetic"
        output.write_bytes(data)
        size = len(data) + (1 if corruption == "size" else 0)
        digest = PRIVATE if corruption == "sha256" else hashlib.sha256(data).hexdigest()
        return size, digest, "18.6"
    monkeypatch.setattr(cli.db_backup, "create_backup", corrupt)
    assert cli.main(_execute(tmp_path / "corrupt.dump")) == 1
    assert harness.calls["restore"] == harness.calls["write"] == 0
    result = capsys.readouterr()
    assert PRIVATE not in result.out + result.err


def test_vector_failure_retains_successful_backup(harness, tmp_path):
    harness.errors.add("first_write")
    output = tmp_path / "retained.dump"
    assert cli.main(_execute(output)) == 1
    assert output.is_file() and output.stat().st_size > 0
    assert harness.rows == {}


def test_owned_restore_marker_failure_never_calls_restore(monkeypatch, tmp_path):
    from scripts import db_restore_smoke
    async def reject(*args, **kwargs):
        raise RuntimeError("ownership mismatch")
    async def forbidden(*args, **kwargs):
        pytest.fail("Restore must not run for an unverified source")
    monkeypatch.setattr(db_restore_smoke, "validate_restore_source", lambda *a, **k: None)
    monkeypatch.setattr(db_restore_smoke, "require_owned_database", reject)
    monkeypatch.setattr(db_restore_smoke, "_restore_verified_source", forbidden)
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        asyncio.run(db_restore_smoke.restore_owned_database_backup(
            tmp_path / "test.dump", owned=object(), config=object()
        ))
