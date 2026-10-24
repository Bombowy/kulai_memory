from __future__ import annotations

import os
import threading
from dataclasses import replace
from uuid import UUID

import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox

from backend.tests.test_desktop_library_ui import library_window, ITEM
from backend.tests.test_desktop_rag_ui import wait_until
from backend.tests.test_desktop_delete import setup
from kulai_memory.desktop.app import MainWindow, AsyncDesktopThread
from kulai_memory.desktop.models import DesktopDeleteProgress, DesktopDeleteProgressState, DesktopMemoryDeleteResult
from kulai_memory.desktop import controller as module
from kulai_memory.application.deletion import MemoryDeletionResult


@pytest.mark.parametrize('archived', [False, True])
def test_one_click_deletes_exact_selection_without_dialog_or_path(monkeypatch, archived):
    app, w, item, _ = library_window(monkeypatch, archived)
    submitted = []

    def unexpected_dialog(*args, **kwargs):
        pytest.fail('DELETE must not open a confirmation, input or file dialog')

    monkeypatch.setattr(QDialog, 'exec', unexpected_dialog)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', unexpected_dialog)
    monkeypatch.setattr(QInputDialog, 'getText', unexpected_dialog)
    monkeypatch.setattr(QMessageBox, 'question', unexpected_dialog)
    monkeypatch.setattr(w._worker, 'request_delete_memory', submitted.append)
    assert not w.library_delete_button.isEnabled()
    w.library_delete_button.click()
    assert submitted == []
    w.library_table.selectRow(0)
    assert w.library_delete_button.isEnabled()
    w.library_delete_button.click()
    assert submitted == [item]
    assert submitted[0].id == item.id and submitted[0].revision == item.revision
    assert w.library_status.text() == 'Deleting memory...'
    w._on_progress(DesktopDeleteProgress(DesktopDeleteProgressState.DELETING))
    assert w.library_status.text() == 'Deleting memory...'
    for button in (w.record_button, w.ask_button, w.voice_ask_button, w.library_edit_button,
                   w.library_archive_button, w.library_restore_button, w.library_delete_button,
                   w.library_refresh_button, w.library_retry_button, w.refresh_button):
        assert not button.isEnabled()
    w.library_delete_button.click()
    assert submitted == [item]
    w.close()
    app.processEvents()


@pytest.mark.parametrize('archived', [False, True])
def test_success_clears_selection_content_and_refreshes_library(monkeypatch, archived):
    app, w, item, refreshes = library_window(monkeypatch, archived)
    other = replace(item, id=UUID(int=2), content='another synthetic memory')
    w._on_library_loaded((item, other))
    w.library_table.selectRow(0)
    w._begin_library_action('Deleting memory...')
    w._on_memory_deleted(DesktopMemoryDeleteResult(item.id, True, int(not archived)))
    assert w.library_content.toPlainText() == '' and w.library_table.rowCount() == 1
    assert w._library_items == (other,) and w._library_selection_id is None
    assert not w.library_delete_button.isEnabled()
    assert refreshes and w.library_status.text() == 'Memory deleted'
    w._on_library_loaded((other,))
    assert w.library_status.text() == 'Memory deleted'
    assert w.record_button.isEnabled() and w.voice_ask_button.isEnabled()
    w.close()
    app.processEvents()


@pytest.mark.parametrize('archived', [False, True])
def test_delete_refreshes_library_and_recent_sequentially_on_real_worker(tmp_path, monkeypatch, archived):
    import asyncio

    from backend.tests.test_desktop_library import harness
    from kulai_memory.application import Memory

    app = QApplication.instance() or QApplication([])
    selected = Memory(id=UUID(int=71), content='synthetic deleted note',
        archived_at=ITEM.created_at if archived else None)
    other = Memory(id=UUID(int=72), content='synthetic remaining active note')
    h, _, _ = harness(tmp_path, monkeypatch, memories=(selected, other))
    deleted, library_entered, release = threading.Event(), threading.Event(), threading.Event()
    controllers, order = [], []
    original_library = h.repository.list_library

    async def read_library(**kwargs):
        if deleted.is_set():
            assert controllers[0]._operation_lock.locked()
            order.append('library:start')
            library_entered.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
        items = await original_library(**kwargs)
        if deleted.is_set():
            order.append('library:end')
        return items

    monkeypatch.setattr(h.repository, 'list_library', read_library)

    def factory(*, progress_callback):
        c = h.controller()
        c._progress_callback = progress_callback
        controllers.append(c)
        original_recent = c.list_recent

        async def read_recent(**kwargs):
            if deleted.is_set():
                assert not c._operation_lock.locked()
                order.append('recent')
            return await original_recent(**kwargs)

        monkeypatch.setattr(c, 'list_recent', read_recent)
        return c

    async def delete(**kwargs):
        assert kwargs == dict(memory_id=selected.id, expected_revision=selected.revision,
                              session_factory=h.sessions)
        assert controllers[0]._operation_lock.locked()
        # Only the isolated in-memory repository is changed; no PostgreSQL DELETE.
        assert h.repository.by_ingestion.pop(selected.ingestion_id) == selected
        deleted.set()
        return MemoryDeletionResult(selected.id, True, int(not archived))

    monkeypatch.setattr(module, 'delete_canonical_memory', delete)
    worker = AsyncDesktopThread(controller_factory=factory)

    def unexpected_separate_recent_request():
        pytest.fail('Refresh both lists in one worker coroutine, without a competing request_recent')

    monkeypatch.setattr(worker, 'request_recent', unexpected_separate_recent_request)
    w = MainWindow(worker=worker, autostart=False)
    failures = QSignalSpy(worker.operation_failed)
    try:
        worker.start()
        wait_until(lambda: w.library_refresh_button.isEnabled() and w.library_table.rowCount() == int(not archived) + 1)
        if archived:
            w.library_filter.setCurrentIndex(1)
            wait_until(lambda: w.library_refresh_button.isEnabled() and w.library_table.rowCount() == 1)
        assert w.recent_table.rowCount() == int(not archived) + 1
        row = next(i for i, item in enumerate(w._library_items) if item.id == selected.id)
        w.library_table.selectRow(row)
        library_signals = QSignalSpy(worker.library_loaded)
        recent_signals = QSignalSpy(worker.recent_loaded)
        delete_signals = QSignalSpy(worker.memory_deleted)
        w.library_delete_button.click()
        wait_until(library_entered.is_set)
        QTest.qWait(30)
        assert order == ['library:start'] and recent_signals.count() == 0
        assert not w.library_refresh_button.isEnabled() and not w.record_button.isEnabled()
        release.set()
        wait_until(lambda: w.library_refresh_button.isEnabled() and not w._busy
                   and delete_signals.count() == library_signals.count() == recent_signals.count() == 1)
        assert order == ['library:start', 'library:end', 'recent']
        assert delete_signals.count() == library_signals.count() == recent_signals.count() == 1
        assert failures.count() == 0
        assert w.library_table.rowCount() == int(not archived)
        assert all(item.id != selected.id for item in w._library_items)
        assert w.recent_table.rowCount() == 1
        assert w.recent_table.item(0, 1).text() == other.content
        assert w.library_status.text() == 'Memory deleted'
    finally:
        release.set()
        worker.request_shutdown()
        wait_until(lambda: not worker.isRunning())
        worker.wait(5000)
        w.close()
        app.processEvents()


@pytest.mark.parametrize('message', [
    'Memory changed. Reload it before deleting.', 'Memory deletion could not be completed.',
])
def test_error_keeps_row_and_conflict_refreshes_without_success_message(monkeypatch, message):
    app, w, item, refreshes = library_window(monkeypatch)
    w.library_table.selectRow(0)
    w._begin_library_action('Deleting memory...')
    w._on_operation_failed('library_delete', message)
    assert bool(refreshes) == message.startswith('Memory changed.')
    assert w.library_table.rowCount() == 1
    assert w.library_status.text() == message
    assert w._library_items == (item,)
    if not refreshes:
        assert w._selected_library_item() == item and w.library_delete_button.isEnabled()
        w._refresh_library(preserve_status=True)
    w._on_library_loaded((replace(item, revision=2),))
    assert w._selected_library_item().revision == 2 and w.library_delete_button.isEnabled()
    w.close()
    app.processEvents()


def test_real_worker_passes_id_revision_and_returns_result(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    h, calls = setup(tmp_path, monkeypatch)
    worker = AsyncDesktopThread(controller_factory=lambda **kw: h.controller())
    results, failures = QSignalSpy(worker.memory_deleted), QSignalSpy(worker.operation_failed)
    try:
        worker.start()
        wait_until(lambda: worker._runtime_ready)
        worker.request_delete_memory(ITEM)
        wait_until(lambda: results.count() == 1)
        assert failures.count() == 0
        assert results.at(0)[0] == DesktopMemoryDeleteResult(ITEM.id, True, 1)
        assert calls == [dict(memory_id=ITEM.id, expected_revision=ITEM.revision, session_factory=h.sessions)]
        assert not list(tmp_path.rglob('*.dump'))
    finally:
        worker.request_shutdown()
        wait_until(lambda: not worker.isRunning())
        worker.wait(5000)


def test_real_worker_close_cancels_delete_without_stale_signals(tmp_path, monkeypatch):
    import asyncio

    app = QApplication.instance() or QApplication([])
    h, calls = setup(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    ui_thread = threading.get_ident()

    async def delete(**kwargs):
        assert threading.get_ident() != ui_thread
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        calls.append('unexpected completed delete')
        return MemoryDeletionResult(kwargs['memory_id'], True, 1)

    monkeypatch.setattr(module, 'delete_canonical_memory', delete)
    worker = AsyncDesktopThread(controller_factory=lambda **kw: h.controller())
    w = MainWindow(worker=worker, autostart=False)
    results, failures = QSignalSpy(worker.memory_deleted), QSignalSpy(worker.operation_failed)
    try:
        worker.start()
        wait_until(lambda: worker._runtime_ready and w._rag_ready and not w._busy)
        worker.request_delete_memory(ITEM)
        wait_until(entered.is_set)
        w.close()
        app.processEvents()
        wait_until(lambda: not worker.isRunning())
        app.processEvents()
        assert w._closing and results.count() == failures.count() == 0 and calls == []
        assert h.embedding.closed == h.rag.closed == 1
    finally:
        release.set()
        worker.request_shutdown()
        worker.wait(5000)
        w.close()
