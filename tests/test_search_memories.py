from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from kulai_vector_store import VectorMetric

from kulai_memory.application import Memory, MemoryRetrievalHit, MemoryRetrievalResult
from kulai_memory.database_safety import CheckResult, DoctorReport
from scripts import search_memories as cli

PRIVATE = "PRIVATE_PROVIDER_PASSWORD_VECTOR_SENTINEL"


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(
        calls={name: 0 for name in ("doctor", "provider", "closed", "engine", "dispose", "retrieve")},
        settings=SimpleNamespace(kulai_embedding_model="bge-m3:567m-fp16",
                                 kulai_vector_dimension=1024, kulai_ollama_base_url="http://127.0.0.1:11434"),
        config=SimpleNamespace(async_url=f"postgresql+asyncpg://u:{PRIVATE}@localhost/kulai_memory"),
        failure=None,
        doctor=DoctorReport((CheckResult("vector.embedding_dimension", True,
                                        {"actual_dimension": 1024, "configured_dimension": 1024}),)),
        result=MemoryRetrievalResult(metric=VectorMetric.COSINE, hits=(MemoryRetrievalHit(
            memory=Memory(id=UUID(int=1), content="synthetic CLI canonical content",
                          metadata={"secret_diagnostic": PRIVATE}),
            rank=1, score=0.875, vector_record_id=str(UUID(int=1)),
        ),)),
    )
    class Provider:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            state.calls["closed"] += 1
    def provider(**kwargs):
        state.calls["provider"] += 1
        if state.failure == "factory":
            raise RuntimeError(PRIVATE)
        return Provider()
    async def doctor(**kwargs):
        state.calls["doctor"] += 1
        return state.doctor
    async def dispose():
        state.calls["dispose"] += 1
    def engine(*args, **kwargs):
        assert kwargs == {"echo": False, "hide_parameters": True}
        state.calls["engine"] += 1
        return SimpleNamespace(dispose=dispose)
    async def retrieve(**kwargs):
        state.calls["retrieve"] += 1
        state.query, state.top_k = kwargs["query"], kwargs["top_k"]
        if state.failure == "retrieve":
            raise RuntimeError(PRIVATE)
        return state.result
    monkeypatch.setattr(cli, "get_settings", lambda: state.settings)
    monkeypatch.setattr(cli, "database_config", lambda: state.config)
    monkeypatch.setattr(cli, "run_database_doctor", doctor)
    monkeypatch.setattr(cli, "create_embedding_provider", provider)
    monkeypatch.setattr(cli, "create_async_engine", engine)
    monkeypatch.setattr(cli, "async_sessionmaker", lambda *a, **k: object())
    monkeypatch.setattr(cli, "retrieve_memories", retrieve)
    return state


def test_explicit_cli_renders_canonical_content_score_and_no_metadata(harness, capsys):
    assert cli.main(["--query", "synthetic query"]) == 0
    assert harness.query == "synthetic query" and harness.top_k == 5
    assert harness.calls == {"doctor": 1, "provider": 1, "closed": 1,
                             "engine": 1, "dispose": 1, "retrieve": 1}
    output = capsys.readouterr()
    assert output.err == ""
    assert "hits=1" in output.out and "score=0.875000000" in output.out
    assert "metric=cosine;score=higher_is_better" in output.out
    assert "content=synthetic CLI canonical content" in output.out
    assert PRIVATE not in output.out and "secret_diagnostic" not in output.out


def test_cli_requested_top_k_and_empty_result(harness, capsys):
    harness.result = MemoryRetrievalResult(metric=VectorMetric.COSINE, hits=())
    assert cli.main(["--query", "synthetic", "--top-k", "20"]) == 0
    assert harness.top_k == 20 and "hits=0" in capsys.readouterr().out


@pytest.mark.parametrize("args", [[], ["--query", PRIVATE, "--top-k", "0"],
                                  ["--query", PRIVATE, "--top-k", "21"],
                                  ["--query", PRIVATE, "--top-k", PRIVATE],
                                  ["--query", PRIVATE, "--unknown", PRIVATE]])
def test_parser_failure_hides_values_and_performs_no_operations(args, harness, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2
    assert all(count == 0 for count in harness.calls.values())
    assert PRIVATE not in capsys.readouterr().err


@pytest.mark.parametrize("query", ["", "  \t", "x" * 10001])
def test_invalid_query_is_rejected_before_doctor(harness, query, capsys):
    assert cli.main(["--query", query]) == 1
    assert harness.calls["doctor"] == harness.calls["provider"] == 0
    assert "retrieval.invalid_query" in capsys.readouterr().err


@pytest.mark.parametrize("guard", ["db_remote", "model", "dimension", "doctor", "schema"])
def test_local_and_schema_guards_block_provider_and_retrieval(guard, harness, capsys):
    if guard == "db_remote":
        harness.config.async_url = f"postgresql+asyncpg://u:{PRIVATE}@192.0.2.1/db"
    elif guard == "model":
        harness.settings.kulai_embedding_model = PRIVATE
    elif guard == "dimension":
        harness.settings.kulai_vector_dimension = 768
    elif guard == "schema":
        harness.doctor = DoctorReport((CheckResult("vector.embedding_dimension", True,
                                                  {"actual_dimension": 768, "configured_dimension": 1024}),))
    else:
        harness.doctor = DoctorReport((CheckResult("database.connection", False, message=PRIVATE),))
    assert cli.main(["--query", "synthetic"]) == 1
    assert harness.calls["provider"] == harness.calls["retrieve"] == 0
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err


def test_real_host_factory_rejects_nonloopback_ollama_without_network(harness, monkeypatch, capsys):
    from kulai_memory.embedding_provider import create_embedding_provider
    monkeypatch.setattr(cli, "create_embedding_provider", create_embedding_provider)
    harness.settings.kulai_ollama_base_url = "http://192.0.2.1:11434"
    assert cli.main(["--query", "synthetic"]) == 1
    assert harness.calls["engine"] == harness.calls["retrieve"] == 0
    assert "192.0.2.1" not in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["factory", "retrieve"])
def test_failure_is_private_and_closes_owned_resources(stage, harness, capsys, caplog):
    harness.failure = stage
    assert cli.main(["--query", "synthetic"]) == 1
    if stage == "retrieve":
        assert harness.calls["closed"] == harness.calls["dispose"] == 1
    else:
        assert harness.calls["engine"] == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert PRIVATE not in output.err + caplog.text
