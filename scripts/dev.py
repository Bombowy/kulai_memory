"""Run the generated backend on localhost for development."""

import argparse
from pathlib import Path

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Start the generated ASGI application without public binding."""

    parser = argparse.ArgumentParser(description="Run the generated backend")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    uvicorn.run("kulai_memory.main:app", host="127.0.0.1", port=args.port, reload=True, app_dir=str(PROJECT_ROOT / "backend/src"))


if __name__ == "__main__":
    main()
