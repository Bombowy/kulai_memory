from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from kulai_embeddings import (
    EmbeddingCapabilities,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingVector,
)

from scripts import embedding_smoke

VECTOR_SENTINEL = 12345.678901
PRIVATE_ERROR_SENTINEL = "private-provider-payload-sentinel"


class _FakeProvider:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities()

    def __init__(self, *, dimension: int = 1024, all_zero: bool = False) -> None:
        self.dimension = dimension
        self.all_zero = all_zero
        self.requests: list[EmbeddingRequest] = []
        self.close_calls = 0
        self.failure: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()

    async def aclose(self):
        self.close_calls += 1

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        value = 0.0 if self.all_zero else VECTOR_SENTINEL
        return EmbeddingResponse(
            provider_id=self.provider_id,
            model_id="bge-m3:567m-fp16",
            embeddings=(EmbeddingVector(values=(value,) * self.dimension),),
            dimension=self.dimension,
        )


def _install_fake(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    settings = SimpleNamespace(
        kulai_embedding_model="bge-m3:567m-fp16", kulai_vector_dimension=1024
    )
    monkeypatch.setattr(embedding_smoke, "get_settings", lambda: settings)

    def create(*, settings: object):
        assert settings is not None
        return provider

    monkeypatch.setattr(embedding_smoke, "create_embedding_provider", create)


def test_smoke_uses_only_synthetic_input_and_reports_no_vector_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = _FakeProvider()
    _install_fake(monkeypatch, provider)

    assert embedding_smoke.main() == 0

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.inputs == ("KulAI Memory embedding dimension verification.",)
    assert request.model_hint is None
    assert request.purpose is None
    assert provider.close_calls == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.splitlines() == [
        "embedding.provider=ollama",
        "embedding.model=bge-m3:567m-fp16",
        "embedding.dimension=1024",
        "embedding.finite=true",
        "embedding.non_zero=true",
        "configured.dimension=1024",
        "status=OK",
    ]
    assert str(VECTOR_SENTINEL) not in output.out
    assert request.inputs[0] not in output.out


def test_smoke_dimension_mismatch_fails_and_closes_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = _FakeProvider(dimension=768)
    _install_fake(monkeypatch, provider)

    assert embedding_smoke.main() == 1

    output = capsys.readouterr()
    assert "dimension validation failed" in output.err
    assert output.out == ""
    assert str(VECTOR_SENTINEL) not in output.err
    assert provider.close_calls == 1


@pytest.mark.parametrize("failure_kind", ["provider", "all_zero"])
def test_smoke_failure_is_private_and_closes_provider(
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _FakeProvider(all_zero=failure_kind == "all_zero")
    if failure_kind == "provider":
        provider.failure = RuntimeError(PRIVATE_ERROR_SENTINEL)
    _install_fake(monkeypatch, provider)

    assert embedding_smoke.main() == 1

    output = capsys.readouterr()
    assert "status=FAIL" in output.err
    assert output.out == ""
    assert PRIVATE_ERROR_SENTINEL not in output.err + caplog.text
    assert str(VECTOR_SENTINEL) not in output.err + caplog.text
    assert provider.close_calls == 1


def test_smoke_hides_unexpected_factory_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake(monkeypatch, _FakeProvider())

    def failed_create(**_kwargs):
        raise RuntimeError(PRIVATE_ERROR_SENTINEL)

    monkeypatch.setattr(embedding_smoke, "create_embedding_provider", failed_create)

    assert embedding_smoke.main() == 1

    output = capsys.readouterr()
    assert "failed safely" in output.err
    assert PRIVATE_ERROR_SENTINEL not in output.err


def test_embedding_smoke_and_host_wiring_have_no_persistence_imports() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "scripts" / "embedding_smoke.py",
        root / "backend" / "src" / "kulai_memory" / "embedding_provider.py",
        root / "backend" / "src" / "kulai_memory" / "embedding_validation.py",
    ]
    forbidden = {
        "sqlalchemy",
        "pgvector",
        "kulai_db",
        "kulai_vector_store",
        "kulai_vector_store_pgvector",
        "kulai_memory.application",
        "kulai_memory.persistence",
        "kulai_memory.database_safety",
    }
    violations = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if any(
                    module == item or module.startswith(item + ".")
                    for item in forbidden
                ):
                    violations.append((path.name, node.lineno, module))
    assert violations == []
