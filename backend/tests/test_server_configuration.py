from __future__ import annotations

import tomllib
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_dev_runtime_installs_uvicorn_websocket_support() -> None:
    metadata = tomllib.loads(
        (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    dev = metadata["project"]["optional-dependencies"]["dev"]

    assert "uvicorn[standard]>=0.34,<1.0" in dev
