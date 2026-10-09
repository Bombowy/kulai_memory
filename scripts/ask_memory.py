"""Read-only local text RAG over canonical KulAI Memory."""

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

from kulai_memory.application.rag import MemoryRagError  # noqa: E402
from kulai_memory.application.retrieval import MemoryRetrievalError, MemoryRetrievalService  # noqa: E402
from kulai_memory.backfill import EMBEDDING_MODEL, VECTOR_DIMENSION  # noqa: E402
from kulai_memory.database_safety import (  # noqa: E402
    SAFE_LOCAL_APP_ENVS, database_config, database_host_is_loopback, run_database_doctor,
)
from kulai_memory.rag_runtime import MemoryRagRuntime  # noqa: E402
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
    result.add_argument("--query", required=True, help="Nonblank question, at most 10000 characters.")
    result.add_argument("--top-k", type=_top_k, default=5)
    result.add_argument("--show-context", action="store_true",
                        help="WARNING: displays user Memory content for local debugging.")
    return result


async def run(args: argparse.Namespace) -> int:
    MemoryRetrievalService.validate_input(query=args.query, top_k=args.top_k)
    settings, config = get_settings(), database_config()
    if (
        settings.app_env.lower() not in SAFE_LOCAL_APP_ENVS
        or not database_host_is_loopback(config)
        or settings.kulai_embedding_model != EMBEDDING_MODEL
        or settings.kulai_vector_dimension != VECTOR_DIMENSION
        or not settings.kulai_llm_model.strip()
    ):
        raise MemoryRagError("rag.invalid_configuration")
    doctor = await run_database_doctor(async_url=config.async_url)
    checks = {check.name: check for check in doctor.checks}
    dimension = checks.get("vector.embedding_dimension")
    current, head = checks.get("alembic.current"), checks.get("alembic.expected_head")
    if (
        not doctor.ok or current is None or head is None
        or current.value != ["kulai_memory_0004"] or head.value != "kulai_memory_0004"
        or dimension is None or not isinstance(dimension.value, dict)
        or dimension.value.get("actual_dimension") != VECTOR_DIMENSION
        or dimension.value.get("configured_dimension") != VECTOR_DIMENSION
    ):
        raise MemoryRagError("rag.invalid_configuration")

    engine = create_async_engine(config.async_url, echo=False, hide_parameters=True)
    try:
        async with MemoryRagRuntime(
            settings=settings, session_factory=async_sessionmaker(engine, expire_on_commit=False),
        ) as runtime:
            result, context = await runtime.ask_with_context(query=args.query, top_k=args.top_k)
    finally:
        await engine.dispose()
    print(f"query={result.query}")
    print(f"answer={result.answer}")
    print(f"sufficient_context={str(result.sufficient_context).lower()}")
    print("citations=" + ",".join(str(citation.memory_id) for citation in result.citations))
    if args.show_context:
        print("context=" + context.text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(asyncio.wait_for(run(args), timeout=300))
    except (MemoryRagError, MemoryRetrievalError) as exc:
        print(f"status=FAIL;code={exc.code}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("status=FAIL;code=interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("status=FAIL;code=rag.operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
