from __future__ import annotations

import asyncio
import math
import os

import pytest
from kulai_embeddings import EmbeddingRequest, embed
from kulai_provider_ollama_embeddings import provider as ollama_provider_module

from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.embedding_validation import validate_embedding_dimension
from kulai_memory.settings import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("KULAI_RUN_OLLAMA_INTEGRATION") != "1",
    reason="set KULAI_RUN_OLLAMA_INTEGRATION=1 for real local Ollama embeddings",
)


def test_native_bge_embeddings_reuse_one_local_provider_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        kulai_embedding_model="bge-m3:567m-fp16",
        kulai_ollama_base_url="http://127.0.0.1:11434",
        kulai_vector_dimension=1024,
    )
    original_client = ollama_provider_module.AsyncClient
    counts = {"created": 0, "requests": 0, "closed": 0, "overrides": 0}

    def tracked_client(*args, **kwargs):
        counts["created"] += 1
        client = original_client(*args, **kwargs)
        original_embed = client.embed
        original_close = client.close

        async def tracked_embed(*args, **kwargs):
            counts["requests"] += 1
            if kwargs.get("dimensions") is not None:
                counts["overrides"] += 1
            return await original_embed(*args, **kwargs)

        async def tracked_close():
            counts["closed"] += 1
            await original_close()

        monkeypatch.setattr(client, "embed", tracked_embed)
        monkeypatch.setattr(client, "close", tracked_close)
        return client

    monkeypatch.setattr(ollama_provider_module, "AsyncClient", tracked_client)

    async def scenario() -> None:
        async with create_embedding_provider(settings=settings) as provider:
            for _ in range(2):
                response = await embed(
                    provider=provider,
                    request=EmbeddingRequest(
                        inputs=("KulAI Memory embedding dimension verification.",)
                    ),
                )
                validate_embedding_dimension(response, expected_dimension=1024)
                # Assert safe metrics, so failures do not render vector objects.
                metrics = {
                    "count": len(response.embeddings),
                    "dimension": response.dimension,
                    "provider": response.provider_id,
                    "model": response.model_id,
                    "finite": all(
                        math.isfinite(value) for value in response.embeddings[0].values
                    ),
                    "non_zero": any(
                        value != 0.0 for value in response.embeddings[0].values
                    ),
                }
                assert metrics == {
                    "count": 1,
                    "dimension": 1024,
                    "provider": "ollama",
                    "model": "bge-m3:567m-fp16",
                    "finite": True,
                    "non_zero": True,
                }

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))

    assert counts == {"created": 1, "requests": 2, "closed": 1, "overrides": 0}
