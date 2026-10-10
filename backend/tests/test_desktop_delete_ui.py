from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog

from backend.tests.test_desktop_library_ui import library_window, ITEM
from backend.tests.test_desktop_rag_ui import wait_until
from backend.tests.test_desktop_delete import setup, SNAPSHOT
from kulai_memory.desktop.app import MemoryDeleteDialog, MainWindow, AsyncDesktopThread
from kulai_memory.desktop.models import DesktopDeleteProgress, DesktopDeleteProgressState, DesktopMemoryDeleteResult
from kulai_memory.desktop import controller as module
from kulai_memory.backup_service import VerifiedBackupResult, drain_before_cancellation


@pytest.mark.parametrize('archived', [False, True])
def test_delete_selection_state_for_active_and_archived(monkeypatch, archived):
    app, w, item, _ = library_window(monkeypatch, archived)
    assert not w.library_delete_button.isEnabled()
    w.library_table.selectRow(0)
    assert w.library_delete_button.isEnabled()
    w._begin_library_action('Busy')
    assert not w.library_delete_button.isEnabled() and not w.record_button.isEnabled()
    assert not w.ask_button.isEnabled() and not w.voice_ask_button.isEnabled()
    w.close()
    app.processEvents()


def test_confirmation_exact_typing_cancel_default_and_no_content():
    app = QApplication.instance() or QApplication([])
    d = MemoryDeleteDialog(ITEM)
    assert str(ITEM.id) in d.warning.text() and 'Revision: 1' in d.warning.text() and 'Status: Active' in d.warning.text()
    assert ITEM.content not in d.warning.text() and 'reversible' in d.warning.text() and 'retained' in d.warning.text()
    assert d.buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
    assert not d.delete_button.isEnabled()
    for wrong in ('delete', 'DELETE ', ' DELETE', 'yes'):
        d.confirmation.setText(wrong)
        assert not d.delete_button.isEnabled()
        d.accept()
        assert d.result() == QDialog.DialogCode.Rejected
    d.confirmation.setText('DELETE')
    assert d.delete_button.isEnabled()
    d.delete_button.click()
    assert d.result() == QDialog.DialogCode.Accepted
    d.close()
    app.processEvents()


@pytest.mark.parametrize('confirmed,path', [(False, ''), (True, ''), (True, 'C:/backups/new.dump')])
def test_confirmation_then_backup_path_required_and_exact_selection(monkeypatch, confirmed, path):
    app, w, item, _ = library_window(monkeypatch)
    submitted, paths = [], []
    monkeypatch.setattr(MemoryDeleteDialog, 'exec', lambda self: QDialog.DialogCode.Accepted if confirmed else QDialog.DialogCode.Rejected)
    def choose(*args, **kw):
        assert 'outside' in args[1] and 'before_delete_' in args[2] and args[2].endswith('.dump')
        paths.append(1)
        return path, ''
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', choose)
    monkeypatch.setattr(w._worker, 'request_delete_memory', lambda selected, output: submitted.append((selected, output)))
    w.library_table.selectRow(0)
    w.library_delete_button.click()
    assert len(paths) == int(confirmed)
    if confirmed and path:
        assert submitted == [(item, Path(path))]
        assert w.library_status.text() == 'Preparing verified backup...'
        for state, text in ((DesktopDeleteProgressState.VERIFYING_BACKUP, 'Verifying backup restore...'),
                            (DesktopDeleteProgressState.DELETING, 'Deleting memory...')):
            w._on_progress(DesktopDeleteProgress(state))
            assert w.library_status.text() == text
        for button in (w.record_button, w.ask_button, w.voice_ask_button, w.library_edit_button,
                       w.library_archive_button, w.library_restore_button, w.library_delete_button,
                       w.library_refresh_button, w.library_retry_button, w.refresh_button):
            assert not button.isEnabled()
    else:
        assert not submitted and w.library_status.text() == 'Delete cancelled' and w.library_delete_button.isEnabled()
    w.close()
    app.processEvents()


def test_success_clears_selection_full_content_and_refreshes_library_recent(monkeypatch, tmp_path):
    app, w, item, refreshes = library_window(monkeypatch)
    w.library_table.selectRow(0)
    w._begin_library_action('Deleting memory...')
    result = DesktopMemoryDeleteResult(item.id, tmp_path / 'retained.dump', 12, 'a' * 64, True, 1)
    w._on_memory_deleted(result)
    assert w.library_content.toPlainText() == '' and w.library_table.rowCount() == 0
    assert w._library_selection_id is None and not w.library_delete_button.isEnabled()
    assert refreshes and 'Memory deleted' in w.library_status.text() and str(result.backup_path) in w.library_status.text()
    w._on_library_loaded(())
    assert w.record_button.isEnabled() and w.voice_ask_button.isEnabled()
    w.close()
    app.processEvents()


def test_revision_conflict_reloads_without_deleting_cached_row(monkeypatch):
    app, w, item, refreshes = library_window(monkeypatch)
    w.library_table.selectRow(0)
    w._begin_library_action('Deleting memory...')
    w._on_operation_failed('library_delete', 'Memory changed. Reload it before deleting.')
    assert refreshes and w.library_table.rowCount() == 1
    assert w.library_status.text() == 'Memory changed. Reload it before deleting.'
    w.close()
    app.processEvents()


def test_real_worker_close_during_admin_drain_no_stale_signals(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    h, calls = setup(tmp_path, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    ui_thread = threading.get_ident()
    async def backup(output, **kw):
        async def work():
            assert threading.get_ident() != ui_thread
            output.write_bytes(b'PGDMP retained')
            entered.set()
            await asyncio.to_thread(release.wait)
            finished.set()
            return VerifiedBackupResult(output, 14, 'a' * 64, SNAPSHOT)
        return await drain_before_cancellation(asyncio.create_task(work()))
    monkeypatch.setattr(module, 'create_verified_backup', backup)
    worker = AsyncDesktopThread(controller_factory=lambda **kw: h.controller())
    w = MainWindow(worker=worker, autostart=False)
    results, failures = QSignalSpy(worker.memory_deleted), QSignalSpy(worker.operation_failed)
    try:
        worker.start()
        wait_until(lambda: worker._runtime_ready and w._rag_ready and not w._busy)
        worker.request_delete_memory(ITEM, tmp_path / 'retained.dump')
        wait_until(entered.is_set)
        w.close()
        app.processEvents()
        assert w._closing and not finished.is_set()
        release.set()
        wait_until(lambda: not worker.isRunning())
        app.processEvents()
        assert finished.is_set() and results.count() == failures.count() == 0 and calls == []
        assert (tmp_path / 'retained.dump').exists() and h.embedding.closed == h.rag.closed == 1
    finally:
        release.set()
        worker.request_shutdown()
        worker.wait(5000)
        w.close()
