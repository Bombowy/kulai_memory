"""Local fact extraction and relation suggestions. Always dry-run, never writes to PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend/src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.application.intelligence import IntelligenceError, SourceDocument  # noqa: E402
from kulai_memory.application.intelligence.models import MAX_SOURCE_CHARS  # noqa: E402
from kulai_memory.memory_intelligence_runtime import MemoryIntelligenceRuntime  # noqa: E402
from kulai_memory.settings import get_settings  # noqa: E402


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        super().error("Invalid preview arguments; use --help. No argument values are logged.")


def _top_k(value: str) -> int:
    try:
        result = int(value)
        if not 1 <= result <= 20:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("Top-k must be between 1 and 20.") from None


def parser() -> argparse.ArgumentParser:
    result = _SafeParser(description=__doc__)
    result.add_argument("--input", type=Path, required=True, help="UTF-8 text file, at most 12000 characters.")
    result.add_argument("--compare-file", type=Path, action="append", default=[])
    result.add_argument("--compare-main", action="store_true", help="Explicit read-only indexed Memory lookup.")
    result.add_argument("--speaker-id", help="Explicit shared narrator identity for all sources; excludes quoted/named people.")
    result.add_argument("--top-k", type=_top_k, default=5)
    result.add_argument("--dry-run", action="store_true", default=True, help="Always enabled; no execute mode exists.")
    result.add_argument("--show-candidates", action="store_true",
                        help="WARNING: displays private claims, evidence, provenance and rationale.")
    return result


def _document(path: Path, *, source_id: str, speaker_id: str | None) -> SourceDocument:
    try:
        # Bound bytes before decoding; preserve CRLF and all original text characters.
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_CHARS * 4 + 4)
            if stream.read(1):
                raise ValueError
        text = data.decode("utf-8-sig")
        if "\0" in text:
            raise ValueError
        return SourceDocument(source_id=source_id, text=text, speaker_id=speaker_id)
    except Exception:
        raise IntelligenceError("input_file_failed") from None


async def run(args: argparse.Namespace) -> int:
    source = _document(args.input, source_id="input", speaker_id=args.speaker_id)
    references = tuple(_document(path, source_id=f"reference-{i}", speaker_id=args.speaker_id)
                       for i, path in enumerate(args.compare_file, 1))
    settings = get_settings()
    # No database configuration, doctor, engine or session is constructed in file-only mode.
    engine = None
    factory = None
    security_status = "NOT_CHECKED_FILE_ONLY"
    try:
        if args.compare_main:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from kulai_memory.database_safety import (
                SAFE_LOCAL_APP_ENVS, database_config, database_host_is_loopback, run_database_doctor,
            )
            config = database_config()
            if settings.app_env.lower() not in SAFE_LOCAL_APP_ENVS or not database_host_is_loopback(config):
                raise IntelligenceError("database_guard_failed")
            doctor = await run_database_doctor(async_url=config.async_url)
            checks = {check.name: check for check in doctor.checks}
            if (
                not doctor.ok
                or checks.get("alembic.current") is None
                or checks["alembic.current"].value != ["kulai_memory_0004"]
                or checks.get("alembic.expected_head") is None
                or checks["alembic.expected_head"].value != "kulai_memory_0004"
                or checks.get("vector.embedding_dimension") is None
                or checks["vector.embedding_dimension"].value != {
                    "configured_dimension": 1024, "actual_dimension": 1024, "actual_type": "vector(1024)",
                }
                or checks.get("security.schema") is None
            ):
                raise IntelligenceError("database_guard_failed")
            security = checks["security.schema"].value
            security_status = "PRESENT" if security["present"] else "ABSENT_SECURE_DOCTOR_FAIL"
            engine = create_async_engine(config.async_url, echo=False, hide_parameters=True)
            factory = async_sessionmaker(engine, expire_on_commit=False)
        async with MemoryIntelligenceRuntime(settings=settings, session_factory=factory) as runtime:
            result = await runtime.preview(source, references, compare_main=args.compare_main, top_k=args.top_k)
    finally:
        if engine is not None:
            await engine.dispose()
    print("status=PREVIEW;dry_run=true;writes=0;review_required=true")
    print(f"scope={result.comparison_scope};exhaustive={str(result.exhaustive).lower()};security_schema={security_status}")
    print(f"sources={len(result.extractions)};compared_pairs={result.compared_pairs};available_pairs={result.available_pairs}")
    for extraction in result.extractions:
        print(f"source={extraction.source_id};candidates={len(extraction.candidates)};rejected={len(extraction.rejections)}")
        for candidate in extraction.candidates:
            print(f"candidate={candidate.candidate_id};type={candidate.fact.information_type};"
                  f"provenance=source_checked;status={candidate.review_status}")
        for rejection in extraction.rejections:
            print(f"rejected={rejection.candidate_id};reason={rejection.reason}")
    for relation in result.relations:
        print(f"pair={relation.left_id},{relation.right_id};relation={relation.relation};"
              f"similarity={relation.similarity};status={relation.review_status};"
              f"validation_notes={','.join(relation.validation_notes)}")
    if args.show_candidates:
        print("PRIVATE_PREVIEW_JSON=" + json.dumps(result.model_dump(mode="json"), ensure_ascii=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except IntelligenceError as exc:
        print(f"status=FAIL;code={exc.code}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("status=FAIL;code=interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("status=FAIL;code=intelligence.operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
