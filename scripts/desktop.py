"""Launch the in-process KulAI Memory desktop application."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def main() -> int:
    try:
        from kulai_memory.desktop.app import main as desktop_main
    except ModuleNotFoundError as exc:
        if exc.name in {"PySide6", "sounddevice"}:
            print(
                'Desktop dependencies are missing; install ".\\backend[desktop]".',
                file=sys.stderr,
            )
            return 2
        raise
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
