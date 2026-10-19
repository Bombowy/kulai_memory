"""Qt-only evidence and ephemeral-state guards, no database or model access."""

import os
from uuid import uuid4
from datetime import UTC, datetime
from copy import deepcopy

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QSignalSpy
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


def long_view():
    import asyncio
    from backend.tests.test_memory_intelligence import GoldenLLM, ConstantBGE, CASES
    from kulai_memory.application.intelligence import MemoryIntelligenceService, SourceDocument
    from kulai_memory.application.fact_memory.models import CandidateRecord, ReviewSnapshot
    from kulai_memory.desktop.fact_review.models import Detail, Row
    case = deepcopy(CASES["pl_flask"])
    case["text"] = "synthetic scroll content\n" * 250 + case["text"]
    source = SourceDocument(source_id="scroll", text=case["text"], memory_id=uuid4(), revision=1)
    extracted = asyncio.run(MemoryIntelligenceService(llm_provider=GoldenLLM(cases=[case]),
        embedding_provider=ConstantBGE()).extract(source))
    record = CandidateRecord(id=uuid4(), fact_id=uuid4(), version=1, source_version_id=uuid4(),
        status="PENDING", lock_version=1, source_valid=True, candidate=extracted.candidates[0], created_at=datetime.now(UTC))
    snapshot = ReviewSnapshot(record=record, source=source, source_token="epoch:synthetic:1")
    detail = Detail(record.id, "PENDING", 1, (snapshot,))
    return View((Row(record.id, "PENDING", 1, ("Synthetic fact", "fact", "backend", "X", "PENDING")),), 1, 1, None, detail)


def test_background_refresh_preserves_document_scroll_cursor_and_selection(app):
    worker = QuietWorker()
    window = FactReviewWindow(worker=worker)
    worker.ready.emit("synthetic-owned")
    view = long_view()
    window._selected = view.detail.id
    worker.completed.emit(window._token, "read", view)
    window.show(); app.processEvents()
    source = window.left.source
    source.verticalScrollBar().setValue(30)
    cursor = source.textCursor()
    cursor.setPosition(10); cursor.setPosition(18, QTextCursor.MoveMode.KeepAnchor)
    source.setTextCursor(cursor)
    scroll, revision, selection = source.verticalScrollBar().value(), source.document().revision(), source.textCursor().selectedText()
    spy = QSignalSpy(source.textChanged)
    window._poll_refresh()
    assert source.toPlainText() and window.table.isEnabled() and not window.approve.isEnabled()
    worker.completed.emit(window._token, "read", deepcopy(view))
    assert spy.count() == 0 and source.document().revision() == revision
    assert source.verticalScrollBar().value() == scroll and source.textCursor().selectedText() == selection
    assert window._selected == view.detail.id and window._lease.isActive()
    # Changed status/history with the SAME immutable source also preserves text.
    snapshot = view.detail.snapshots[0]
    updated_record = snapshot.record.model_copy(update={"status": "APPROVED", "lock_version": 2})
    from dataclasses import replace
    updated = replace(view, rows=(), pending_count=0, total_count=0,
        detail=replace(view.detail, status="APPROVED", lock_version=2,
            snapshots=(snapshot.model_copy(update={"record": updated_record}),)))
    window._poll_refresh()
    worker.completed.emit(window._token, "read", updated)
    assert spy.count() == 0 and source.verticalScrollBar().value() == scroll
    assert source.textCursor().selectedText() == selection and window.revoke.isEnabled()
    window._poll_refresh()
    worker.completed.emit(window._token, "read", View((), 0, 0, None, stale_selection=True))
    assert not source.toPlainText() and not source.extraSelections() and window._detail is None
    window.close(); app.processEvents()


def test_expired_lease_discards_inflight_snapshot_and_failure_clears_immediately(app):
    worker = QuietWorker()
    window = FactReviewWindow(worker=worker)
    worker.ready.emit("synthetic-owned")
    view = long_view()
    window._selected = view.detail.id
    worker.completed.emit(window._token, "read", view)
    window._poll_refresh()
    old_token = window._token
    window._expire()
    assert not window.left.source.toPlainText() and window._selected is None
    worker.completed.emit(old_token, "read", view)
    assert window._view is None and not window.left.source.toPlainText()
    app.processEvents()
    worker.completed.emit(window._token, "read", view)
    window._poll_refresh()
    worker.failed.emit(window._token, "read", "Safe database failure")
    assert window._view is None and not window.left.source.toPlainText() and not window.history.toPlainText()
    window.close(); app.processEvents()
