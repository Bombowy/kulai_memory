"""Launch isolated TEST DATA fact review; no ports, main writes or repo export."""

import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)  # --help never provisions a database.
    try:
        from PySide6.QtWidgets import QApplication
        from kulai_memory.desktop.fact_review.window import FactReviewWindow
    except ImportError:
        print("Brak zależności desktop. Użyj istniejącego backend/.venv z PySide6.", file=sys.stderr)
        return 2
    app = QApplication.instance() or QApplication([])
    window = FactReviewWindow()
    window.show()
    try:
        result = app.exec()
    finally:
        window.worker.request_shutdown()
        window.worker.wait()  # Qt loop has exited; never terminate a DB cleanup thread.
    return result if window.worker.clean_shutdown else 1


if __name__ == "__main__":
    raise SystemExit(main())
