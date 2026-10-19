"""Interactive synthetic Fact-RAG with human decisions and an owned disposable DB."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-models", action="store_true", help="Use existing local Qwen/BGE for ASK, no downloads.")
    args = parser.parse_args(argv)
    from kulai_memory.fact_rag_demo import console
    from kulai_memory.application.approved_facts import FactRagError
    from kulai_memory.application.fact_memory import FactMemoryError
    try:
        return asyncio.run(console(real_models=args.real_models))
    except (FactRagError, FactMemoryError) as exc:
        print(exc.code, file=sys.stderr)
    except KeyboardInterrupt:
        print("Demo interrupted; owned cleanup requested.", file=sys.stderr)
    except Exception:
        print("Fact-RAG demo failed safely.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
