"""Run the generated backend on localhost for development."""

import argparse
import os
from pathlib import Path

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Start the generated ASGI application without public binding."""

    parser = argparse.ArgumentParser(description="Run the generated backend")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--security-mode", choices=("LOCAL_DEV", "SECURE"), default=None)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    from kulai_memory.settings import get_settings

    settings = get_settings()
    # Explicit dev launcher opts into local compatibility only if not configured.
    # An existing SECURE environment/.env setting is never silently downgraded.
    if args.security_mode is not None:
        os.environ["KULAI_SECURITY_MODE"] = args.security_mode
    elif "kulai_security_mode" not in settings.model_fields_set:
        os.environ["KULAI_SECURITY_MODE"] = "LOCAL_DEV"
    get_settings.cache_clear()
    print(f"KulAI security mode: {get_settings().kulai_security_mode.value}; loopback only", flush=True)
    uvicorn.run("kulai_memory.main:app", host="127.0.0.1", port=args.port, reload=True,
                app_dir=str(PROJECT_ROOT / "backend/src"), proxy_headers=False,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
