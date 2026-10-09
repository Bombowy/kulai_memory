from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from backend.tests.test_desktop_rag_ui import ready_window, wait_until
from backend.tests.test_desktop_library import harness
from kulai_memory.application.indexing import IndexReconciliationReport
from kulai_memory.desktop.app import MemoryEditDialog, AsyncDesktopThread, MainWindow
from kulai_memory.desktop.models import DesktopMemoryItem, DesktopMemoryFilter, DesktopMemoryChangeStatus, DesktopMemoryChangeResult

ITEM = DesktopMemoryItem(UUID(int=1), datetime(2025, 1, 1, tzinfo=UTC), 1, False, 'full content\n' + 'x' * 300)


def library_window(monkeypatch, archived=False):
    app, window, _, _ = ready_window(monkeypatch)
    calls = []
    monkeypatch.setattr(window._worker, 'request_library', lambda filter, limit: calls.append((filter, limit)))
    if archived:
        window.library_filter.blockSignals(True)
        window.library_filter.setCurrentIndex(1)
        window.library_filter.blockSignals(False)
    item = replace(ITEM, archived=archived)
    window._on_library_loaded((item,))
    return app, window, item, calls


def test_library_widgets_selection_full_content_and_filter_refresh(monkeypatch):
    app, w, item, calls = library_window(monkeypatch)
    assert w.library_group.title() == 'Memory Library'
    assert [w.library_filter.itemText(i) for i in range(2)] == ['Active', 'Archived']
    assert w.library_content.isReadOnly() and w.library_table.columnCount() == 4
    assert not w.library_edit_button.isEnabled() and not w.library_archive_button.isEnabled() and not w.library_restore_button.isEnabled()
    assert len(w.library_table.item(0, 3).text()) <= 160
    w.library_table.selectRow(0)
    assert w.library_content.toPlainText() == item.content
    assert w.library_edit_button.isEnabled() and w.library_archive_button.isEnabled() and not w.library_restore_button.isEnabled()
    w.library_filter.setCurrentIndex(1)
    assert calls == [(DesktopMemoryFilter.ARCHIVED, 100)]
    assert not w.record_button.isEnabled() and not w.ask_button.isEnabled() and not w.voice_ask_button.isEnabled()
    w._on_library_loaded((replace(item, archived=True),))
    w.library_table.selectRow(0)
    assert w.library_restore_button.isEnabled() and not w.library_edit_button.isEnabled() and not w.library_archive_button.isEnabled()
    w.close()
    app.processEvents()


@pytest.mark.parametrize('accepted', [True, False])
def test_editor_submits_observed_id_revision_and_exact_content(monkeypatch, accepted):
    app, w, item, _ = library_window(monkeypatch)
    calls = []
    monkeypatch.setattr(w._worker, 'request_edit_memory', lambda selected, text: calls.append((selected, text)))
    def execute(dialog):
        assert dialog.editor.toPlainText() == item.content
        assert not w.ask_button.isEnabled() and not w.record_button.isEnabled()
        dialog.editor.selectAll()
        dialog.editor.insertPlainText('  changed\ncontent  ')
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
    monkeypatch.setattr(MemoryEditDialog, 'exec', execute)
    w.library_table.selectRow(0)
    w.library_edit_button.click()
    assert calls == ([(item, '  changed\ncontent  ')] if accepted else [])
    if not accepted:
        assert w.record_button.isEnabled() and w.library_edit_button.isEnabled()
    w.close()
    app.processEvents()


def test_editor_noop_preserves_original_newlines_and_blank_save_disabled():
    app = QApplication.instance() or QApplication([])
    dialog = MemoryEditDialog(replace(ITEM, content='original\r\ntext'))
    assert dialog.content == 'original\r\ntext'
    dialog.editor.selectAll()
    dialog.editor.insertPlainText('  \n')
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
    dialog.close()


@pytest.mark.parametrize('confirmed', [True, False])
def test_archive_requires_confirmation_and_only_submits_uuid(monkeypatch, confirmed):
    app, w, item, _ = library_window(monkeypatch)
    calls = []
    monkeypatch.setattr(w._worker, 'request_archive_memory', calls.append)
    def question(parent, title, text, choices, default):
        assert text == 'Archive this memory?' and default == QMessageBox.StandardButton.No
        return QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, 'question', question)
    w.library_table.selectRow(0)
    w.library_archive_button.click()
    assert calls == ([item.id] if confirmed else [])
    w.close()
    app.processEvents()


def test_restore_submits_selected_archived_id_and_removes_row_after_success(monkeypatch):
    app, w, item, reads = library_window(monkeypatch, archived=True)
    calls = []
    monkeypatch.setattr(w._worker, 'request_restore_memory', calls.append)
    w.library_table.selectRow(0)
    w.library_restore_button.click()
    assert calls == [item.id]
    w._on_library_changed(DesktopMemoryChangeResult(replace(item, archived=False), DesktopMemoryChangeStatus.RESTORED))
    assert w.library_table.rowCount() == 0 and reads == [(DesktopMemoryFilter.ARCHIVED, 100)]
    w._on_library_loaded(())
    assert not w.library_restore_button.isEnabled() and w.record_button.isEnabled()
    w.close()
    app.processEvents()


@pytest.mark.parametrize('restored', [False, True])
def test_degraded_indexing_renders_durable_canonical_and_retry(monkeypatch, restored):
    app, w, item, reads = library_window(monkeypatch)
    w.library_table.selectRow(0)
    changed = replace(item, revision=2, content='Jupiter')
    w._on_library_changed(DesktopMemoryChangeResult(changed,
        DesktopMemoryChangeStatus.RESTORED if restored else DesktopMemoryChangeStatus.EDITED, True))
    assert w._library_items[0] == changed and w.library_table.item(0, 1).text() == '2'
    message = 'Memory was restored, but semantic indexing needs retry.' if restored else 'Memory was updated, but semantic indexing needs retry.'
    assert w.library_status.text() == message and reads == [(DesktopMemoryFilter.ACTIVE, 100)]
    w._on_library_loaded((changed,))
    assert w.library_content.toPlainText() == 'Jupiter' and not w.library_retry_button.isHidden()
    calls = []
    monkeypatch.setattr(w._worker, 'request_library_indexing_retry', lambda: calls.append(True))
    w.library_retry_button.click()
    assert calls == [True] and not w.library_edit_button.isEnabled()
    w._on_library_indexing_finished(IndexReconciliationReport(selected=1, indexed=1))
    w._on_library_loaded((changed,))
    assert w.library_retry_button.isHidden() and w.library_status.text() == 'Semantic indexing is current'
    w.close()
    app.processEvents()


def test_conflict_reloads_selected_memory_and_read_failure_clears_stale_content(monkeypatch):
    app, w, item, reads = library_window(monkeypatch)
    w.library_table.selectRow(0)
    w._begin_library_action('Editing')
    message = 'Memory changed. Reload it before editing.'
    w._on_operation_failed('library_edit', message)
    assert w.library_status.text() == message and reads == [(DesktopMemoryFilter.ACTIVE, 100)]
    current = replace(item, revision=3, content='latest canonical')
    w._on_library_loaded((current,))
    assert w.library_content.toPlainText() == current.content and w.library_edit_button.isEnabled()
    w._on_operation_failed('library_read', 'Memory Library could not be loaded.')
    assert w.library_table.rowCount() == 0 and w.library_content.toPlainText() == ''
    assert not w.library_edit_button.isEnabled() and w.record_button.isEnabled()
    w.close()
    app.processEvents()


def test_real_worker_library_read_edit_refresh_and_close_cancels_indexing(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    h, memory, _ = harness(tmp_path, monkeypatch)
    errors = []
    def factory(*, progress_callback):
        asyncio.get_event_loop().set_exception_handler(lambda loop, context: errors.append(context))
        c = h.controller()
        c._progress_callback = progress_callback
        return c
    worker = AsyncDesktopThread(controller_factory=factory)
    w = MainWindow(worker=worker)
    success = QSignalSpy(worker.library_changed)
    try:
        w.show()
        wait_until(lambda: w.library_table.rowCount() == 1 and w.library_refresh_button.isEnabled())
        async def block(**kwargs):
            await asyncio.sleep(180)
        h.indexer.ensure = block
        worker.request_edit_memory(w._library_items[0], 'Jupiter')
        wait_until(lambda: bool(h.repository.saved))
        w.close()
        wait_until(lambda: not worker.isRunning())
        assert not errors and success.count() == 0
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == 1
        assert h.repository.saved[0].revision == 2
    finally:
        worker.request_shutdown()
        assert worker.wait(2000)
        w.close()
