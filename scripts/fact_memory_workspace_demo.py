"""One isolated TEST DATA desktop for fact review and approved Fact-RAG."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'/'src'))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-models',action='store_true',help='Use installed Qwen/BGE for ASK/grounding; never download.')
    args=parser.parse_args(argv)
    from PySide6.QtWidgets import QApplication
    from kulai_memory.desktop.fact_workspace.window import FactWorkspaceWindow
    app=QApplication.instance() or QApplication([])
    window=FactWorkspaceWindow(real_models=args.real_models)
    window.show()
    try: result=app.exec()
    finally:
        window.worker.request_shutdown()
        window.worker.wait()
    return result if window.worker.clean_shutdown else 1


if __name__=='__main__': raise SystemExit(main())
