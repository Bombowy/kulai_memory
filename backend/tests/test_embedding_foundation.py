from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest
from kulai_embeddings import (
    EmbeddingCapabilities,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingResponseError,
    EmbeddingValidationError,
    EmbeddingVector,
    embed,
)

from kulai_memory import embedding_provider
from kulai_memory.embedding_validation import validate_embedding_dimension
from kulai_memory.settings import Settings

VECTOR_SENTINEL = 12345.678901


def _response(dimension: int = 1024) -> EmbeddingResponse:
    return EmbeddingResponse(
        provider_id="fake-embeddings",
        embeddings=(EmbeddingVector(values=(VECTOR_SENTINEL,) * dimension),),
        dimension=dimension,
        model_id="fake-model",
    )


class _FakeProvider:
    provider_id = "fake-embeddings"
    capabilities = EmbeddingCapabilities()

    def __init__(self, response: EmbeddingResponse) -> None:
        self.response = response

    async def embed(self, _request: EmbeddingRequest) -> EmbeddingResponse:
        return self.response


def test_embedding_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KULAI_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("KULAI_OLLAMA_BASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.kulai_embedding_model == "bge-m3:567m-fp16"
    assert settings.kulai_ollama_base_url == "http://127.0.0.1:11434"


def test_embedding_settings_follow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KULAI_EMBEDDING_MODEL", "configured-model:exact-tag")
    monkeypatch.setenv("KULAI_OLLAMA_BASE_URL", "http://localhost:11435")

    settings = Settings(_env_file=None)

    assert settings.kulai_embedding_model == "configured-model:exact-tag"
    assert settings.kulai_ollama_base_url == "http://localhost:11435"


@pytest.mark.parametrize(
    "host",
    ["http://127.0.0.1:11434", "http://localhost:11435", "http://[::1]:11434"],
)
def test_factory_passes_exact_settings_without_dimension_override(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_configs = []
    sentinel_provider = object()

    def create(config):
        captured_configs.append(config)
        return sentinel_provider

    monkeypatch.setattr(embedding_provider, "OllamaEmbeddingProvider", create)
    settings = Settings(
        _env_file=None,
        kulai_embedding_model="configured-model:exact-tag",
        kulai_ollama_base_url=host,
        kulai_vector_dimension=1024,
    )

    provider = embedding_provider.create_embedding_provider(settings=settings)

    assert provider is sentinel_provider
    assert len(captured_configs) == 1
    config = captured_configs[0]
    assert config.model == "configured-model:exact-tag"
    assert config.host == host
    assert config.dimensions is None
    assert config.truncate is False
    assert config.allow_model_hint is False


@pytest.mark.parametrize(
    "host",
    [
        "http://example.invalid:11434",
        "http://192.0.2.1:11434",
        "http://user:PRIVATE_CONFIG_SENTINEL@127.0.0.1:11434",
        "not-a-url-PRIVATE_CONFIG_SENTINEL",
    ],
)
def test_factory_rejects_invalid_or_nonlocal_hosts_before_client_creation(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_create(_config):
        pytest.fail("Invalid/nonlocal configuration must not create an HTTP client")

    monkeypatch.setattr(embedding_provider, "OllamaEmbeddingProvider", forbidden_create)
    settings = Settings(_env_file=None, kulai_ollama_base_url=host)

    with pytest.raises(EmbeddingValidationError) as caught:
        embedding_provider.create_embedding_provider(settings=settings)

    assert host not in str(caught.value)
    assert "PRIVATE_CONFIG_SENTINEL" not in str(caught.value)


def test_host_declares_embedding_dependencies_and_leaves_sdk_to_provider() -> None:
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependencies = tomllib.loads(path.read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]

    assert "kulai-embeddings>=0.1.0" in dependencies
    assert "kulai-provider-ollama-embeddings>=0.1.0" in dependencies
    assert not any(item.startswith(("ollama", "httpx")) for item in dependencies)


def test_provider_neutral_flow_and_1024_dimension_validation() -> None:
    response = _response()

    result = asyncio.run(
        embed(
            provider=_FakeProvider(response),
            request=EmbeddingRequest(inputs=("Synthetic embedding test.",)),
        )
    )

    assert result.dimension == 1024
    assert validate_embedding_dimension(result, expected_dimension=1024) is None
    assert result.embeddings[0].values == response.embeddings[0].values


def test_dimension_mismatch_has_safe_controlled_error() -> None:
    with pytest.raises(EmbeddingValidationError) as caught:
        validate_embedding_dimension(_response(768), expected_dimension=1024)

    assert caught.value.public_details == {
        "expected_dimension": 1024,
        "actual_dimension": 768,
    }
    assert "does not match" in str(caught.value)
    assert str(VECTOR_SENTINEL) not in str(caught.value)
    assert str(VECTOR_SENTINEL) not in repr(caught.value.public_details)


@pytest.mark.parametrize("expected_dimension", [None, 0, -1, True])
def test_host_requires_explicit_positive_dimension(expected_dimension) -> None:
    with pytest.raises(EmbeddingValidationError, match="explicitly configured"):
        validate_embedding_dimension(_response(), expected_dimension=expected_dimension)


@pytest.mark.parametrize(
    "values", [(), (float("nan"),), (float("inf"),), (-float("inf"),)]
)
def test_reusable_service_revalidates_invalid_vectors_without_leaking_values(
    values: tuple[float, ...], caplog: pytest.LogCaptureFixture
) -> None:
    # Bypass construction deliberately: the orchestration service must revalidate.
    vector = EmbeddingVector.model_construct(values=(VECTOR_SENTINEL, *values))
    if not values:
        vector = EmbeddingVector.model_construct(values=())
    response = EmbeddingResponse.model_construct(
        provider_id="fake-embeddings",
        embeddings=(vector,),
        dimension=len(vector.values),
        model_id="fake-model",
        usage=None,
    )

    with pytest.raises(EmbeddingResponseError) as caught:
        asyncio.run(
            embed(
                provider=_FakeProvider(response),
                request=EmbeddingRequest(inputs=("Synthetic embedding test.",)),
            )
        )

    assert str(VECTOR_SENTINEL) not in str(caught.value)
    assert str(VECTOR_SENTINEL) not in caplog.text
    assert caught.value.public_details == {}


def test_reusable_service_rejects_empty_embedding_batch() -> None:
    response = EmbeddingResponse.model_construct(
        provider_id="fake-embeddings",
        embeddings=(),
        dimension=1024,
        model_id="fake-model",
        usage=None,
    )

    with pytest.raises(EmbeddingResponseError):
        asyncio.run(
            embed(
                provider=_FakeProvider(response),
                request=EmbeddingRequest(inputs=("Synthetic embedding test.",)),
            )
        )
