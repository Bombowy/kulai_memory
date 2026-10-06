"""Probe a local native embedding with synthetic text and no persistence."""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from kulai_embeddings import (  # noqa: E402
    EmbeddingError,
    EmbeddingRequest,
    EmbeddingResponseError,
    EmbeddingValidationError,
    embed,
)

from kulai_memory.embedding_provider import create_embedding_provider  # noqa: E402
from kulai_memory.embedding_validation import validate_embedding_dimension  # noqa: E402
from kulai_memory.settings import get_settings  # noqa: E402

SYNTHETIC_TEXT = "KulAI Memory embedding dimension verification."


async def _run() -> None:
    settings = get_settings()
    async with create_embedding_provider(settings=settings) as provider:
        response = await embed(
            provider=provider,
            request=EmbeddingRequest(inputs=(SYNTHETIC_TEXT,)),
        )
        validate_embedding_dimension(
            response, expected_dimension=settings.kulai_vector_dimension
        )
        values = response.embeddings[0].values
        finite = all(math.isfinite(value) for value in values)
        non_zero = any(value != 0.0 for value in values)
        if not non_zero:
            raise EmbeddingResponseError("Embedding probe returned an all-zero vector.")

    print(f"embedding.provider={response.provider_id}")
    print(f"embedding.model={response.model_id or settings.kulai_embedding_model}")
    print(f"embedding.dimension={response.dimension}")
    print(f"embedding.finite={str(finite).lower()}")
    print(f"embedding.non_zero={str(non_zero).lower()}")
    print(f"configured.dimension={settings.kulai_vector_dimension}")
    print("status=OK")


def main() -> int:
    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=120))
    except EmbeddingValidationError:
        print(
            "status=FAIL: embedding configuration or dimension validation failed.",
            file=sys.stderr,
        )
        return 1
    except EmbeddingError:
        print("status=FAIL: embedding provider failed.", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("status=FAIL: embedding smoke failed safely.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
