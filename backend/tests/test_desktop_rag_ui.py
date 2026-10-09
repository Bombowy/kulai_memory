from __future__ import annotations

import asyncio
import os
import threading
from uuid import UUID

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from backend.tests.test_desktop_controller import Harness
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.desktop.app import AsyncDesktopThread, MainWindow
from kulai_memory.desktop.models import (
    DesktopRagCitation, DesktopRagProgress, DesktopRagProgressState, DesktopRagResult,
    DesktopRagStatus, DesktopStartupResult, MicrophoneDevice,
)


def wait_until(condition):
    for _ in range(200):
        if condition():
            return
        QTest.qWait(10)
    assert condition()


def ready_window(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(autostart=False)
    submitted, recent = [], []
    monkeypatch.setattr(window._worker, 'request_ask_memory', lambda query, top_k: submitted.append((query, top_k)))
    monkeypatch.setattr(window._worker, 'request_recent', lambda: recent.append(1))
    window._on_startup_ready(DesktopStartupResult(devices=(MicrophoneDevice(3, 'Fake', None, True),), memories=()))
    return app, window, submitted, recent


def test_ask_widgets_validation_exact_submission_and_states(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(autostart=False)
    assert window.ask_group.title() == 'Ask Memory'
    assert window.answer.isReadOnly() and window.sources_table.columnCount() == 3
    assert not window.ask_button.isEnabled() and not window.query_input.isEnabled()
    window._on_startup_ready(DesktopStartupResult(devices=(), memories=()))
    assert window.query_input.isEnabled() and not window.record_button.isEnabled()
    submitted = []
    monkeypatch.setattr(window._worker, 'request_ask_memory', lambda query, top_k: submitted.append((query, top_k)))
    window.query_input.setText('  ')
    window._ask_memory()
    assert not submitted and not window.ask_button.isEnabled()
    window.query_input.setText(' Gdzie mieszka zielony smok? ')
    assert window.ask_button.isEnabled()
    window.answer.setPlainText('old answer')
    window.sources_table.setRowCount(1)
    window.ask_button.click()
    assert submitted == [(' Gdzie mieszka zielony smok? ', 5)]
    assert window.answer.toPlainText() == '' and window.sources_table.rowCount() == 0
    assert window.rag_status.text() == 'Searching memory...'
    assert not window.ask_button.isEnabled() and not window.query_input.isEnabled()
    window._on_progress(DesktopRagProgress(DesktopRagProgressState.GENERATING))
    assert window.rag_status.text() == 'Generating answer...'
    window.close()
    app.processEvents()


@pytest.mark.parametrize('status,answer,message', [
    (DesktopRagStatus.ANSWERED, 'Wenus', 'Answered'),
    (DesktopRagStatus.INSUFFICIENT_CONTEXT, INSUFFICIENT_CONTEXT_ANSWER, 'Not enough information'),
    (DesktopRagStatus.FAILED, '', 'An answer could not be generated.'),
])
def test_result_rendering_citations_and_controls_without_recent_refresh(monkeypatch, status, answer, message):
    app, window, submitted, recent = ready_window(monkeypatch)
    window.query_input.setText('question')
    window.ask_button.click()
    assert not window.record_button.isEnabled() and not window.refresh_button.isEnabled()
    citations = (DesktopRagCitation(UUID(int=9), 3, 0.876543),) if status is DesktopRagStatus.ANSWERED else ()
    window._on_rag_finished(DesktopRagResult(status, answer, citations, message if status is DesktopRagStatus.FAILED else None))
    assert window.answer.toPlainText() == answer and window.rag_status.text() == message
    assert window.sources_table.rowCount() == len(citations)
    if citations:
        assert [window.sources_table.item(0, i).text() for i in range(3)] == ['3', str(UUID(int=9)), '0.8765']
    assert window.ask_button.isEnabled() and window.query_input.isEnabled() and window.record_button.isEnabled()
    assert recent == []
    window.close()
    app.processEvents()


def test_worker_failure_clears_previous_result_and_voice_blocks_ask(monkeypatch):
    app, window, submitted, _ = ready_window(monkeypatch)
    window.query_input.setText('question')
    window._on_recording_started(UUID(int=1))
    assert window.stop_button.isEnabled() and not window.ask_button.isEnabled()
    window._ask_memory()
    assert submitted == []
    window._recording = False
    window._elapsed_timer.stop()
    window._update_controls()
    window.answer.setPlainText('old answer')
    window.ask_button.click()
    window._on_operation_failed('ask', 'Memory search could not be completed.')
    assert window.answer.toPlainText() == '' and window.sources_table.rowCount() == 0
    assert window.rag_status.text() == 'Memory search could not be completed.'
    assert window.ask_button.isEnabled() and window.record_button.isEnabled()
    window.close()
    app.processEvents()


def test_real_worker_loop_keeps_qt_responsive_and_close_cancels_ask(tmp_path):
    app = QApplication.instance() or QApplication([])
    h = Harness(tmp_path)
    cancelled = threading.Event()
    request_threads, pending_errors = [], []
    async def block():
        request_threads.append(threading.get_ident())
        try:
            await asyncio.sleep(180)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    h.rag.before = block
    def controller_factory(*, progress_callback):
        asyncio.get_event_loop().set_exception_handler(lambda loop, context: pending_errors.append(context))
        c = h.controller()
        c._progress_callback = progress_callback
        return c
    worker = AsyncDesktopThread(controller_factory=controller_factory)
    success, failure, shutdown = QSignalSpy(worker.rag_finished), QSignalSpy(worker.operation_failed), QSignalSpy(worker.shutdown_finished)
    window = MainWindow(worker=worker)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    try:
        window.show()
        wait_until(lambda: window.query_input.isEnabled())
        window.query_input.setText('question')
        window.ask_button.click()
        wait_until(lambda: window.rag_status.text() == 'Generating answer...')
        assert h.rag.requests == [('question', 5)]
        tick_count = len(ticks)
        QTest.qWait(40)
        assert len(ticks) > tick_count
        assert request_threads and request_threads[0] != threading.get_ident()
        window.close()
        wait_until(lambda: shutdown.count() == 1 and not worker.isRunning())
        assert cancelled.is_set() and success.count() == failure.count() == 0
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == 1
        assert worker._loop is None and worker._controller is None
        assert not pending_errors
        app.processEvents()
        assert not window.isVisible()
    finally:
        timer.stop()
        worker.request_shutdown()
        assert worker.wait(2000)
        window.close()
