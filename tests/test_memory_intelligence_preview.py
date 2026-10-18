from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.tests.test_memory_intelligence import CASES, PRIVATE, ConstantBGE, GoldenLLM
from kulai_memory import memory_intelligence_runtime as host
from kulai_memory.application.intelligence import IntelligenceError, SourceDocument
from kulai_memory.settings import Settings
from scripts import memory_intelligence_preview as cli


@pytest.fixture
def local(monkeypatch, tmp_path):
    state = SimpleNamespace(llm=GoldenLLM(relation="possible_supersession", replacement_side="left"), bge=ConstantBGE())
    monkeypatch.setattr(host, "create_llm_provider", lambda **kw: state.llm)
    monkeypatch.setattr(host, "create_embedding_provider", lambda **kw: state.bge)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))
    from kulai_memory import database_safety
    def forbidden(*args, **kwargs):
        pytest.fail("File-only preview must not configure or access PostgreSQL")
    monkeypatch.setattr(database_safety, "database_config", forbidden)
    monkeypatch.setattr(database_safety, "run_database_doctor", forbidden)
    state.input = tmp_path / "source.txt"
    state.reference = tmp_path / "previous.txt"
    state.input.write_text(CASES["pl_switch"]["text"], encoding="utf-8")
    state.reference.write_text(CASES["pl_flask"]["text"], encoding="utf-8")
    return state


def test_cli_is_usable_and_default_output_contains_no_claims(local, capsys, caplog):
    assert cli.main(["--input", str(local.input), "--compare-file", str(local.reference), "--dry-run"]) == 0
    output = capsys.readouterr()
    assert "dry_run=true;writes=0" in output.out and "possible_supersession" in output.out
    assert "provenance=source_checked" in output.out and not output.err
    assert CASES["pl_switch"]["text"] not in output.out + caplog.text
    assert "PRIVATE_PREVIEW_JSON" not in output.out
    assert local.llm.closed == local.bge.closed == 1


def test_full_output_requires_explicit_option(local, capsys):
    assert "private claims" in cli.parser().format_help()
    assert cli.main(["--input", str(local.input), "--show-candidates"]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE_PREVIEW_JSON=" in output and '"memory_id": null' in output
    assert '"review_status": "requires_review"' in output


@pytest.mark.parametrize("args", [[], ["--input", PRIVATE, "--top-k", PRIVATE],
    ["--input", PRIVATE, "--top-k", "0"], ["--input", PRIVATE, "--top-k", "21"],
    ["--input", PRIVATE, "--execute"], ["--unknown", PRIVATE]])
def test_parser_never_echoes_private_arguments(args, capsys):
    with pytest.raises(SystemExit):
        cli.main(args)
    assert PRIVATE not in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "binary", "too_long", "bad_utf8", "bad_speaker"])
def test_bad_input_before_provider_or_database(local, capsys, kind):
    if kind == "missing": local.input.unlink()
    if kind == "binary": local.input.write_bytes(b"abc\0def")
    if kind == "too_long": local.input.write_text("a" * 12001)
    if kind == "bad_utf8": local.input.write_bytes(b"\xff\xff")
    args = ["--input", str(local.input)]
    if kind == "bad_speaker": args += ["--speaker-id", "a" * 129]
    assert cli.main(args) == 1
    assert not local.llm.calls and not local.bge.calls
    assert "input_file_failed" in capsys.readouterr().err


def test_utf8_bom_crlf_are_not_normalized(tmp_path):
    path = tmp_path / "text.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "Żółć\r\nDrugi wiersz".encode("utf-8"))
    source = cli._document(path, source_id="a", speaker_id=None)
    assert source.text == "Żółć\r\nDrugi wiersz"


@pytest.mark.parametrize("failure", ["factory", "generation", "cancel"])
def test_runtime_closes_owned_clients_on_failure(local, monkeypatch, failure):
    if failure == "factory":
        def broken(**kwargs): raise RuntimeError(PRIVATE)
        monkeypatch.setattr(host, "create_llm_provider", broken)
    else:
        async def broken(request):
            if failure == "cancel": raise asyncio.CancelledError()
            raise RuntimeError(PRIVATE)
        local.llm.generate = broken
    async def scenario():
        async with host.MemoryIntelligenceRuntime(settings=Settings(_env_file=None)) as runtime:
            await runtime.preview(SourceDocument(source_id="a", text=CASES["pl_flask"]["text"]))
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else IntelligenceError) as caught:
        asyncio.run(scenario())
    assert PRIVATE not in str(caught.value)
    assert local.bge.closed == 1
    assert local.llm.closed == (0 if failure == "factory" else 1)


def test_cli_failure_is_private_and_cleanup_complete(local, capsys, caplog):
    async def broken(request): raise RuntimeError(PRIVATE)
    local.llm.generate = broken
    assert cli.main(["--input", str(local.input)]) == 1
    output = capsys.readouterr()
    assert not output.out and PRIVATE not in output.err + caplog.text
    assert local.llm.closed == local.bge.closed == 1


@pytest.mark.parametrize("changes", [dict(kulai_llm_model="qwen3:14b"), dict(kulai_embedding_model="other"),
    dict(kulai_vector_dimension=768), dict(kulai_ollama_base_url="https://example.org")])
def test_wrong_models_dimensions_or_remote_host_fail_safely(changes):
    async def scenario():
        async with host.MemoryIntelligenceRuntime(settings=Settings(_env_file=None, **changes)):
            pytest.fail("Invalid provider configuration was accepted")
    with pytest.raises(IntelligenceError, match="invalid_configuration"):
        asyncio.run(scenario())


@pytest.mark.parametrize("guard", ["environment", "remote", "doctor", "head", "revision", "dimension", "security"])
def test_main_guards_fail_before_provider_startup(local, monkeypatch, capsys, guard):
    from kulai_memory import database_safety as safety
    from kulai_memory.database_safety import CheckResult, DoctorReport
    settings = Settings(_env_file=None, kulai_vector_dimension=1024)
    if guard == "environment": settings.app_env = "production"
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(safety, "database_config", lambda: SimpleNamespace(async_url="postgresql+asyncpg://localhost/synthetic_owned"))
    monkeypatch.setattr(safety, "database_host_is_loopback", lambda config: guard != "remote")
    checks = [
        CheckResult("alembic.current", True, ["kulai_memory_0004"]),
        CheckResult("alembic.expected_head", True, "kulai_memory_0004"),
        CheckResult("vector.embedding_dimension", True, dict(configured_dimension=1024, actual_dimension=1024, actual_type="vector(1024)")),
        CheckResult("security.schema", True, dict(present=False, valid=True)),
    ]
    if guard == "doctor": checks.append(CheckResult("database.connection", False))
    if guard == "head": checks[1] = CheckResult("alembic.expected_head", True, "kulai_memory_0005")
    if guard == "revision": checks[0] = CheckResult("alembic.current", True, ["kulai_memory_0003"])
    if guard == "dimension": checks[2] = CheckResult("vector.embedding_dimension", True, {})
    if guard == "security": checks.pop()
    async def doctor(**kwargs): return DoctorReport(tuple(checks))
    monkeypatch.setattr(safety, "run_database_doctor", doctor)
    assert cli.main(["--input", str(local.input), "--compare-main"]) == 1
    assert not local.llm.calls and not local.bge.calls
    assert "database_guard_failed" in capsys.readouterr().err


def test_top_k_coverage_and_source_identity_are_explicit():
    from backend.tests.test_memory_intelligence import service
    source = SourceDocument(source_id="a", text=CASES["pl_multi"]["text"])
    result = asyncio.run(service().preview(source, top_k=1))
    assert not result.exhaustive and result.compared_pairs < result.available_pairs
    with pytest.raises(IntelligenceError, match="invalid_input"):
        asyncio.run(service().preview(source, (source,)))


@pytest.mark.parametrize("failure", [False, True])
def test_main_cli_success_or_failure_disposes_engine_and_reports_security(local, monkeypatch, capsys, failure):
    from kulai_memory import database_safety as safety
    from kulai_memory.database_safety import CheckResult, DoctorReport
    from backend.tests.test_memory_rag import evidence
    from sqlalchemy.ext import asyncio as sql_async
    disposed = []
    async def dispose(): disposed.append(True)
    monkeypatch.setattr(safety, "database_config", lambda: SimpleNamespace(async_url="postgresql+asyncpg://localhost/synthetic_owned"))
    monkeypatch.setattr(safety, "database_host_is_loopback", lambda config: True)
    async def doctor(**kwargs):
        return DoctorReport((
            CheckResult("alembic.current", True, ["kulai_memory_0004"]),
            CheckResult("alembic.expected_head", True, "kulai_memory_0004"),
            CheckResult("vector.embedding_dimension", True, dict(configured_dimension=1024, actual_dimension=1024, actual_type="vector(1024)")),
            CheckResult("security.schema", True, dict(present=False, valid=True)),
        ))
    monkeypatch.setattr(safety, "run_database_doctor", doctor)
    monkeypatch.setattr(sql_async, "create_async_engine", lambda *args, **kwargs: SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(sql_async, "async_sessionmaker", lambda *args, **kwargs: object())
    async def retrieve(**kwargs):
        if failure: raise RuntimeError(PRIVATE)
        return evidence(CASES["pl_flask"]["text"])
    monkeypatch.setattr(host, "retrieve_memories", retrieve)
    assert cli.main(["--input", str(local.input), "--compare-main"]) == int(failure)
    output = capsys.readouterr()
    assert disposed == [True] and local.llm.closed == local.bge.closed == 1
    assert PRIVATE not in output.out + output.err
    if not failure:
        assert "local_and_indexed_main" in output.out and "exhaustive=false" in output.out
        assert "ABSENT_SECURE_DOCTOR_FAIL" in output.out
