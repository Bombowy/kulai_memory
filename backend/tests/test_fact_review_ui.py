"""Qt-only evidence and ephemeral-state guards, no database or model access."""

import os
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from kulai_memory.application.intelligence.models import EvidenceSpan
from kulai_memory.desktop.fact_review.evidence import qt_text_and_positions, set_evidence
from kulai_memory.desktop.fact_review.models import Intent, Outcome, View, safe_message
from kulai_memory.desktop.fact_review.window import FactReviewWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("prefix,quote,suffix", [
    ("Zażółć ", "gęślą jaźń", "!"),
    ("🧠 ", "𐐷🙂", " koniec"),
    ("e\u0301\r\n", "Flask", "\r\nFlask"),
    ("a\r\n", "b\r\nc", "\r\nd"),
    ("a\r", "b", "\rc"),
    ("a\u2028", "b", "\u2029c"),
])
def test_exact_unicode_qt_highlighting(app, prefix, quote, suffix):
    source = prefix + quote + suffix
    span = EvidenceSpan(quote=quote, start=len(prefix), end=len(prefix) + len(quote))
    editor = QPlainTextEdit()
    set_evidence(editor, source, (span,))
    normalized, positions = qt_text_and_positions(source)
    selections = editor.extraSelections()  # Keep the owning Qt value alive.
    selection = selections[0].cursor
    assert editor.toPlainText() == normalized
    assert selection.selectionStart() == positions[span.start]
    assert selection.selectionEnd() == positions[span.end]
    expected = quote.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    assert selection.selectedText().replace("\u2029", "\n") == expected


def test_repeated_quotes_use_each_exact_offset_and_mismatch_erases_text(app):
    source = "🙂 Flask\r\nFlask oraz Flask"
    starts = [source.index("Flask"), source.index("Flask", 8), source.rindex("Flask")]
    spans = tuple(EvidenceSpan(quote="Flask", start=n, end=n + 5) for n in starts)
    editor = QPlainTextEdit()
    set_evidence(editor, source, spans)
    assert len(editor.extraSelections()) == 3
    assert len({s.cursor.selectionStart() for s in editor.extraSelections()}) == 3
    with pytest.raises(ValueError):
        set_evidence(editor, source, (EvidenceSpan(quote="Flask", start=0, end=5),))
    assert not editor.toPlainText() and not editor.extraSelections()


class QuietWorker(QObject):
    ready = Signal(str)
    completed = Signal(int, str, object)
    failed = Signal(int, str, str)
    finished = Signal()
    clean_shutdown = True

    def __init__(self):
        super().__init__()
        self.calls = []

    def start(self):
        pass

    def submit(self, *args):
        self.calls.append(args)
        return True

    def request_shutdown(self):
        self.finished.emit()


def test_unknown_request_state_retains_uuid_and_scrubs_all_ui(app):
    worker = QuietWorker()
    window = FactReviewWindow(worker=worker)
    worker.ready.emit("synthetic-owned")
    token = window._token
    worker.completed.emit(token, "read", View((), 0, 0, None))
    intent = Intent(uuid4(), "candidate", "approve", 1, uuid4())
    window._pending_intent = intent
    window.left.source.setPlainText("PRIVATE_QUOTE_SENTINEL")
    window.history.setPlainText("PRIVATE_QUOTE_SENTINEL")
    window._send("decide", intent)
    worker.completed.emit(window._token, "decide", Outcome("uncertain"))
    assert window._pending_intent is intent
    assert not window.left.source.toPlainText() and not window.history.toPlainText()
    assert not window.approve.isEnabled()
    # Retry resolves/reuses the SAME immutable intent, never a fresh request ID.
    window._busy = False
    window.retry.click()
    assert worker.calls[-1][1:] == ("decide", intent)
    worker.failed.emit(window._token, "decide", "Safe failure")
    assert window._pending_intent is intent and not window.left.source.toPlainText()
    window.close()
    app.processEvents()


def test_safe_errors_never_include_provider_payload():
    private = "PRIVATE_QUOTE_SENTINEL"
    assert private not in safe_message(RuntimeError(private))


def test_old_filter_response_never_repaints_a_cleared_panel(app):
    worker = QuietWorker()
    window = FactReviewWindow(worker=worker)
    worker.ready.emit("synthetic-owned")
    old_token = window._token
    window.type_filter.setCurrentIndex(window.type_filter.findData("plan"))
    worker.completed.emit(old_token, "read", View((), 999, 999, None))
    assert window._view is None and "999" not in window.counts.text()
    app.processEvents()
    worker.completed.emit(window._token, "read", View((), 0, 0, None))
    assert window._view.pending_count == 0
    window.close()
    app.processEvents()
