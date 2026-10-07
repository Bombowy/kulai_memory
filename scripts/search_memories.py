"""Explicit local Memory search; canonical content is shown only on success."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from kulai_memory.application import (  # noqa: E402
    MemoryRetrievalError, MemoryRetrievalResult, MemoryRetrievalService,
)
from kulai_memory.backfill import EMBEDDING_MODEL, VECTOR_DIMENSION  # noqa: E402
from kulai_memory.database_safety import (  # noqa: E402
    database_config, database_host_is_loopback, run_database_doctor,
)
from kulai_memory.embedding_provider import create_embedding_provider  # noqa: E402
from kulai_memory.retrieval_persistence import retrieve_memories  # noqa: E402
from kulai_memory.settings import get_settings  # noqa: E402


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        super().error("Provide --query TEXT and --top-k as an integer between 1 and 20.")


def _top_k(value: str) -> int:
    try:
        number = int(value)
        if not 1 <= number <= 20:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("Top-k must be between 1 and 20.") from None


def parser() -> argparse.ArgumentParser:
    result = _SafeParser(description=__doc__)
    result.add_argument("--query", required=True)
    result.add_argument("--top-k", type=_top_k, default=5)
    return result


def _print_result(*, query: str, result: MemoryRetrievalResult) -> None:
    # Explicit local CLI may display content. Automated main smoke uses the adapter directly.
    print(f"query={query}")
    print(f"hits={len(result.hits)}")
    print(f"metric={result.metric.value};score=higher_is_better")
    for hit in result.hits:
        print(f"\n#{hit.rank}")
        print(f"score={hit.score:.9f}")
        print(f"memory_id={hit.memory.id}")
        print(f"created_at={hit.memory.created_at.isoformat()}")
        print(f"content={hit.memory.content}")


async def run(args: argparse.Namespace) -> int:
    MemoryRetrievalService.validate_input(query=args.query, top_k=args.top_k)
    settings = get_settings()
    config = database_config()
    if (
        not database_host_is_loopback(config)
        or settings.kulai_embedding_model != EMBEDDING_MODEL
        or settings.kulai_vector_dimension != VECTOR_DIMENSION
    ):
        raise MemoryRetrievalError()
    doctor = await run_database_doctor(async_url=config.async_url)
    dimension = next((check for check in doctor.checks
                      if check.name == "vector.embedding_dimension"), None)
    if not doctor.ok or dimension is None or not isinstance(dimension.value, dict) or (
        dimension.value.get("actual_dimension") != VECTOR_DIMENSION
        or dimension.value.get("configured_dimension") != VECTOR_DIMENSION
    ):
        raise MemoryRetrievalError()

    engine = None
    try:
        async with create_embedding_provider(settings=settings) as provider:
            engine = create_async_engine(config.async_url, echo=False, hide_parameters=True)
            service = MemoryRetrievalService(
                provider=provider, expected_dimension=VECTOR_DIMENSION,
                expected_provider_id="ollama", expected_model_tag=EMBEDDING_MODEL,
            )
            result = await retrieve_memories(
                query=args.query, top_k=args.top_k, service=service,
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
            )
    finally:
        if engine is not None:
            await engine.dispose()
    _print_result(query=args.query, result=result)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(asyncio.wait_for(run(args), timeout=120))
    except MemoryRetrievalError as exc:
        print(f"status=FAIL;code={exc.code}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("status=FAIL;code=interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("status=FAIL;code=retrieval.operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
