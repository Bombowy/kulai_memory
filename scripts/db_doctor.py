"""Check KulAI Memory database invariants without changing the database."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.database_safety import run_database_doctor


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--json", action="store_true", dest="as_json")
    return result


async def run(*, as_json: bool) -> int:
    report = await run_database_doctor()
    if as_json:
        print(json.dumps(report.as_json_compatible(), sort_keys=True))
    else:
        for check in report.checks:
            status = "OK" if check.ok else "FAIL"
            suffix = f": {check.value}" if check.value is not None else ""
            if check.message:
                suffix += f" ({check.message})"
            print(f"[{status}] {check.name}{suffix}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(as_json=args.as_json))
    except Exception as exc:
        print(f"Database doctor failed safely ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
