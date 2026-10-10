"""PySide6 desktop window and its dedicated asyncio worker thread."""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from PySide6.QtCore import QElapsedTimer, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog, QDialogButtonBox, QMessageBox, QScrollArea, QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .controller import DesktopController
from .models import (
    DesktopProcessingResult,
    DesktopProgress,
    DesktopProgressState,
    DesktopPublicError,
    DesktopResultStatus,
    DesktopStartupResult,
    MemorySummary,
    DesktopRagProgress, DesktopRagProgressState, DesktopRagResult, DesktopRagStatus,
    DesktopVoiceQuestionProgress, DesktopVoiceQuestionProgressState, DesktopVoiceQuestionResult,
    DesktopMemoryFilter, DesktopMemoryItem, DesktopMemoryChangeResult, DesktopMemoryChangeStatus,
    DesktopMemoryDeleteResult, DesktopDeleteProgress, DesktopDeleteProgressState,
)
from .recorder import DEFAULT_MAX_DURATION_SECONDS


ControllerFactory = Callable[..., DesktopController]


class AsyncDesktopThread(QThread):
    """Keep one controller on one asyncio loop outside the Qt UI thread."""

    startup_ready = Signal(object)
    startup_failed = Signal(str)
    recording_started = Signal(object)
    progress = Signal(object)
    processing_finished = Signal(object)
    rag_finished = Signal(object)
    voice_question_started = Signal()
    voice_question_finished = Signal(object)
    recent_loaded = Signal(object)
    library_loaded = Signal(object)
    library_changed = Signal(object)
    library_indexing_finished = Signal(object)
    memory_deleted = Signal(object)
    operation_failed = Signal(str, str)
    shutdown_finished = Signal()

    def __init__(
        self,
        *,
        controller_factory: ControllerFactory = DesktopController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller_factory = controller_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._controller: DesktopController | None = None
        self._runtime_ready = False
        self._shutdown_requested = threading.Event()
        self._rag_future: Future[Any] | None = None

    def _emit_progress(self, progress: object) -> None:
        if not self._shutdown_requested.is_set():
            self.progress.emit(progress)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        controller: DesktopController | None = None
        try:
            try:
                controller = self._controller_factory(
                    progress_callback=self._emit_progress
                )
                self._controller = controller
                startup = loop.run_until_complete(controller.startup())
            except Exception as exc:
                self.startup_failed.emit(_public_message(exc))
                if controller is not None:
                    loop.run_until_complete(controller.shutdown())
                return
            if self._shutdown_requested.is_set():
                loop.run_until_complete(controller.shutdown())
                return
            self._runtime_ready = True
            self.startup_ready.emit(startup)
            loop.run_forever()
        finally:
            try:
                self._runtime_ready = False
                if controller is not None and not loop.is_closed():
                    loop.run_until_complete(controller.shutdown())
                if not loop.is_closed():
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
            finally:
                self._controller = None
                self._loop = None
                loop.close()
                asyncio.set_event_loop(None)
                if self._shutdown_requested.is_set():
                    self.shutdown_finished.emit()

    def request_start_recording(self, device_id: int) -> None:
        self._submit(
            "record",
            lambda controller: controller.start_recording(device_id=device_id),
            self.recording_started.emit,
        )

    def request_stop_and_process(self) -> None:
        self._submit(
            "process",
            lambda controller: controller.stop_and_process(),
            self.processing_finished.emit,
        )

    def request_retry_save(self) -> None:
        self._submit(
            "retry_save",
            lambda controller: controller.retry_save(),
            self.processing_finished.emit,
        )

    def request_recent(self) -> None:
        self._submit(
            "recent",
            lambda controller: controller.list_recent(),
            self.recent_loaded.emit,
        )

    def request_ask_memory(self, query: str, top_k: int = 5) -> None:
        self._rag_future = self._submit(
            "ask", lambda controller: controller.ask_memory(query=query, top_k=top_k),
            self.rag_finished.emit,
        )

    def request_start_voice_question(self, device_id: int) -> None:
        self._submit("voice_question_start",
                     lambda controller: controller.start_voice_question(device_id=device_id),
                     lambda ignored: self.voice_question_started.emit())

    def request_stop_voice_question_and_ask(self, top_k: int = 5) -> None:
        self._rag_future = self._submit("voice_question",
            lambda controller: controller.stop_voice_question_and_ask(top_k=top_k),
            self.voice_question_finished.emit)

    def request_library(self, filter: DesktopMemoryFilter, limit: int = 100) -> None:
        async def read(controller):
            items = await controller.list_memory_library(filter=filter, limit=limit)
            recent = await controller.list_recent()
            if not self._shutdown_requested.is_set():
                self.recent_loaded.emit(recent)
            return items
        self._submit("library_read", read, self.library_loaded.emit)

    def request_edit_memory(self, item: DesktopMemoryItem, content: str) -> None:
        self._submit("library_edit", lambda c: c.edit_memory(memory_id=item.id,
            expected_revision=item.revision, content=content), self.library_changed.emit)

    def request_archive_memory(self, memory_id) -> None:
        self._submit("library_archive", lambda c: c.archive_memory(memory_id=memory_id), self.library_changed.emit)

    def request_restore_memory(self, memory_id) -> None:
        self._submit("library_restore", lambda c: c.restore_memory(memory_id=memory_id), self.library_changed.emit)

    def request_library_indexing_retry(self) -> None:
        self._submit("library_retry", lambda c: c.reconcile_missing_indexes(), self.library_indexing_finished.emit)

    def request_delete_memory(self, item: DesktopMemoryItem, backup_output: Path) -> None:
        self._submit("library_delete", lambda c: c.delete_memory(memory_id=item.id,
            expected_revision=item.revision, backup_output=backup_output), self.memory_deleted.emit)

    def request_shutdown(self) -> None:
        if self._shutdown_requested.is_set():
            return
        self._shutdown_requested.set()
        loop = self._loop
        controller = self._controller
        if (
            loop is None
            or controller is None
            or loop.is_closed()
            or not self._runtime_ready
        ):
            return
        asyncio.run_coroutine_threadsafe(self._shutdown(controller), loop)

    async def _shutdown(self, controller: DesktopController) -> None:
        try:
            await controller.shutdown()
        finally:
            loop = asyncio.get_running_loop()
            loop.call_soon(loop.stop)

    def _submit(
        self,
        operation: str,
        create: Callable[[DesktopController], Coroutine[Any, Any, Any]],
        success: Callable[[object], None],
    ) -> Future[Any] | None:
        loop = self._loop
        controller = self._controller
        if self._shutdown_requested.is_set():
            return
        if loop is None or controller is None or loop.is_closed() or not self._runtime_ready:
            self.operation_failed.emit(operation, "Desktop runtime is not ready.")
            return
        return asyncio.run_coroutine_threadsafe(
            self._execute(operation, create(controller), success),
            loop,
        )

    async def _execute(
        self,
        operation: str,
        coroutine: Coroutine[Any, Any, Any],
        success: Callable[[object], None],
    ) -> None:
        if self._shutdown_requested.is_set():
            coroutine.close()
            return
        try:
            result = await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._shutdown_requested.is_set():
                self.operation_failed.emit(operation, _public_message(exc))
        else:
            if not self._shutdown_requested.is_set():
                success(result)


def _public_message(exc: Exception) -> str:
    if isinstance(exc, DesktopPublicError):
        return exc.public_message
    return "The desktop operation could not be completed."


class MemoryEditDialog(QDialog):
    """Qt-only editor holding the canonical revision observed when opened."""

    def __init__(self, item: DesktopMemoryItem, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Memory")
        self.resize(640, 420)
        self._original = item.content
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Memory {item.id} — revision {item.revision}"))
        self.editor = QPlainTextEdit(self)
        self.editor.setPlainText(item.content)
        self.editor.document().setModified(False)
        layout.addWidget(self.editor)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.editor.textChanged.connect(lambda: self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(
            bool(self.editor.toPlainText().strip())))
        layout.addWidget(self.buttons)

    @property
    def content(self) -> str:
        return self.editor.toPlainText() if self.editor.document().isModified() else self._original


class MemoryDeleteDialog(QDialog):
    """Explicit destruction confirmation; never displays canonical content."""

    def __init__(self, item: DesktopMemoryItem, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Delete Memory permanently")
        layout = QVBoxLayout(self)
        self.warning = QLabel(
            f"Delete this memory permanently?\n\nMemory UUID: {item.id}\nRevision: {item.revision}\n"
            f"Status: {'Archived' if item.archived else 'Active'}\n\n"
            "Archive is a reversible alternative. Delete is permanent in the canonical database.\n"
            "Recovery requires the retained PostgreSQL backup. A full backup and an actual\n"
            "restore verification must succeed before deletion.\n\nType exactly DELETE to continue.", self)
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.confirmation = QLineEdit(self)
        layout.addWidget(self.confirmation)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.delete_button = self.buttons.addButton("Delete permanently", QDialogButtonBox.ButtonRole.AcceptRole)
        self.delete_button.setAutoDefault(False)
        self.delete_button.setEnabled(False)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        cancel.setFocus()
        self.confirmation.textChanged.connect(lambda text: self.delete_button.setEnabled(text == "DELETE"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def accept(self) -> None:
        if self.confirmation.text() == "DELETE":
            super().accept()


class MainWindow(QMainWindow):
    """Separate voice notes and read-only text/voice questions on one worker."""

    def __init__(
        self,
        *,
        worker: AsyncDesktopThread | None = None,
        autostart: bool = True,
    ) -> None:
        super().__init__()
        self.setWindowTitle("KulAI Memory")
        self.resize(920, 880)
        self._worker = worker or AsyncDesktopThread(parent=self)
        self._recording = False
        self._ready = False
        self._rag_ready = False
        self._busy = False
        self._rag_active = False
        self._voice_question_active = False
        self._question_recording = False
        self._library_items: tuple[DesktopMemoryItem, ...] = ()
        self._library_active = False
        self._library_selection_id = None
        self._library_indexing_degraded = False
        self._save_pending = False
        self._closing = False
        self._shutdown_done = False
        self._elapsed = QElapsedTimer()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._question_timer = QTimer(self)
        self._question_timer.setSingleShot(True)
        self._question_timer.timeout.connect(self._toggle_voice_question)

        self._build_ui()
        self._connect_worker()
        if autostart:
            self._worker.start()

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)

        capture = QGroupBox("Voice note", root)
        capture_layout = QGridLayout(capture)
        capture_layout.addWidget(QLabel("Microphone:"), 0, 0)
        self.device_selector = QComboBox(capture)
        capture_layout.addWidget(self.device_selector, 0, 1, 1, 3)

        self.record_button = QPushButton("NAGRAJ", capture)
        self.stop_button = QPushButton("STOP", capture)
        self.elapsed_label = QLabel("00:00", capture)
        capture_layout.addWidget(self.record_button, 1, 0)
        capture_layout.addWidget(self.stop_button, 1, 1)
        capture_layout.addWidget(QLabel("Elapsed:"), 1, 2)
        capture_layout.addWidget(self.elapsed_label, 1, 3)
        layout.addWidget(capture)

        self.status_label = QLabel("Starting...", root)
        layout.addWidget(self.status_label)
        layout.addWidget(QLabel("Transcript:"))
        self.transcript = QPlainTextEdit(root)
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Your transcript will appear here.")
        layout.addWidget(self.transcript)

        save_row = QHBoxLayout()
        self.save_status = QLabel("", root)
        self.retry_button = QPushButton("RETRY SAVE", root)
        self.retry_button.setVisible(False)
        save_row.addWidget(self.save_status)
        save_row.addStretch(1)
        save_row.addWidget(self.retry_button)
        layout.addLayout(save_row)

        self.ask_group = QGroupBox("Ask Memory", root)
        ask_layout = QVBoxLayout(self.ask_group)
        question_row = QHBoxLayout()
        self.query_input = QLineEdit(self.ask_group)
        self.query_input.setPlaceholderText("Ask something about your memories...")
        self.query_input.setMaxLength(10000)
        self.ask_button = QPushButton("ASK", self.ask_group)
        self.voice_ask_button = QPushButton("ASK BY VOICE", self.ask_group)
        question_row.addWidget(self.query_input, 1)
        question_row.addWidget(self.ask_button)
        question_row.addWidget(self.voice_ask_button)
        ask_layout.addLayout(question_row)
        self.rag_status = QLabel("Starting...", self.ask_group)
        ask_layout.addWidget(self.rag_status)
        self.answer = QPlainTextEdit(self.ask_group)
        self.answer.setReadOnly(True)
        self.answer.setPlaceholderText("An answer grounded in your memories will appear here.")
        ask_layout.addWidget(self.answer)
        ask_layout.addWidget(QLabel("Sources", self.ask_group))
        self.sources_table = QTableWidget(0, 3, self.ask_group)
        self.sources_table.setHorizontalHeaderLabels(["Rank", "Memory ID", "Score"])
        self.sources_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sources_table.horizontalHeader().setStretchLastSection(True)
        self.sources_table.setMaximumHeight(140)
        ask_layout.addWidget(self.sources_table)
        layout.addWidget(self.ask_group)

        self.library_group = QGroupBox("Memory Library", root)
        library_layout = QVBoxLayout(self.library_group)
        library_header = QHBoxLayout()
        self.library_filter = QComboBox(self.library_group)
        self.library_filter.addItem("Active", DesktopMemoryFilter.ACTIVE)
        self.library_filter.addItem("Archived", DesktopMemoryFilter.ARCHIVED)
        self.library_refresh_button = QPushButton("REFRESH", self.library_group)
        library_header.addWidget(self.library_filter)
        library_header.addStretch(1)
        library_header.addWidget(self.library_refresh_button)
        library_layout.addLayout(library_header)
        self.library_table = QTableWidget(0, 4, self.library_group)
        self.library_table.setHorizontalHeaderLabels(["Created", "Revision", "Status", "Content preview"])
        self.library_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.library_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.library_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.library_table.horizontalHeader().setStretchLastSection(True)
        self.library_table.setMaximumHeight(220)
        library_layout.addWidget(self.library_table)
        self.library_content = QPlainTextEdit(self.library_group)
        self.library_content.setReadOnly(True)
        self.library_content.setPlaceholderText("Select a memory to read its full content.")
        self.library_content.setMaximumHeight(200)
        library_layout.addWidget(self.library_content)
        action_row = QHBoxLayout()
        self.library_edit_button = QPushButton("EDIT", self.library_group)
        self.library_archive_button = QPushButton("ARCHIVE", self.library_group)
        self.library_restore_button = QPushButton("RESTORE", self.library_group)
        self.library_delete_button = QPushButton("DELETE", self.library_group)
        self.library_retry_button = QPushButton("RETRY INDEXING", self.library_group)
        for button in (self.library_edit_button, self.library_archive_button,
                       self.library_restore_button, self.library_delete_button, self.library_retry_button):
            action_row.addWidget(button)
        library_layout.addLayout(action_row)
        self.library_status = QLabel("Starting...", self.library_group)
        self.library_status.setWordWrap(True)
        library_layout.addWidget(self.library_status)
        layout.addWidget(self.library_group)

        recent_header = QHBoxLayout()
        recent_header.addWidget(QLabel("Recent Memory"))
        recent_header.addStretch(1)
        self.refresh_button = QPushButton("Refresh", root)
        recent_header.addWidget(self.refresh_button)
        layout.addLayout(recent_header)
        self.recent_table = QTableWidget(0, 2, root)
        self.recent_table.setHorizontalHeaderLabels(["Created", "Content"])
        self.recent_table.horizontalHeader().setStretchLastSection(True)
        self.recent_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.recent_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        layout.addWidget(self.recent_table)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)

        self.record_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.record_button.clicked.connect(self._start_recording)
        self.stop_button.clicked.connect(self._stop_recording)
        self.retry_button.clicked.connect(self._retry_save)
        self.refresh_button.clicked.connect(self._worker.request_recent)
        self.query_input.textChanged.connect(self._update_controls)
        self.query_input.returnPressed.connect(self._ask_memory)
        self.ask_button.clicked.connect(self._ask_memory)
        self.voice_ask_button.clicked.connect(self._toggle_voice_question)
        self.library_filter.currentIndexChanged.connect(self._refresh_library)
        self.library_refresh_button.clicked.connect(self._refresh_library)
        self.library_table.itemSelectionChanged.connect(self._library_selection_changed)
        self.library_edit_button.clicked.connect(self._edit_library_memory)
        self.library_archive_button.clicked.connect(self._archive_library_memory)
        self.library_restore_button.clicked.connect(self._restore_library_memory)
        self.library_delete_button.clicked.connect(self._delete_library_memory)
        self.library_retry_button.clicked.connect(self._retry_library_indexing)
        self._update_controls()

    def _connect_worker(self) -> None:
        self._worker.startup_ready.connect(self._on_startup_ready)
        self._worker.startup_failed.connect(self._on_startup_failed)
        self._worker.recording_started.connect(self._on_recording_started)
        self._worker.progress.connect(self._on_progress)
        self._worker.processing_finished.connect(self._on_processing_finished)
        self._worker.rag_finished.connect(self._on_rag_finished)
        self._worker.voice_question_started.connect(self._on_voice_question_started)
        self._worker.voice_question_finished.connect(self._on_voice_question_finished)
        self._worker.recent_loaded.connect(self._set_recent)
        self._worker.library_loaded.connect(self._on_library_loaded)
        self._worker.library_changed.connect(self._on_library_changed)
        self._worker.library_indexing_finished.connect(self._on_library_indexing_finished)
        self._worker.memory_deleted.connect(self._on_memory_deleted)
        self._worker.operation_failed.connect(self._on_operation_failed)
        self._worker.shutdown_finished.connect(self._on_shutdown_finished)
        self._worker.finished.connect(self._on_shutdown_finished)

    @Slot()
    def _update_controls(self) -> None:
        blocked = (self._busy or self._recording or self._voice_question_active
                   or self._save_pending or self._closing)
        self.record_button.setEnabled(self._ready and not blocked)
        self.query_input.setEnabled(self._rag_ready and not blocked)
        self.ask_button.setEnabled(self._rag_ready and not blocked and bool(self.query_input.text().strip()))
        self.voice_ask_button.setText("STOP QUESTION" if self._question_recording else "ASK BY VOICE")
        self.voice_ask_button.setEnabled(
            (self._question_recording and not self._busy and not self._closing)
            or (self._ready and self._rag_ready and not blocked))
        self.refresh_button.setEnabled(self._rag_ready and not self._busy and not self._recording
                                       and not self._voice_question_active and not self._closing)
        self.device_selector.setEnabled(not blocked)
        library_ready = self._rag_ready and not blocked
        item = self._selected_library_item()
        self.library_filter.setEnabled(library_ready)
        self.library_refresh_button.setEnabled(library_ready)
        self.library_table.setEnabled(library_ready)
        self.library_edit_button.setEnabled(library_ready and item is not None and not item.archived)
        self.library_archive_button.setEnabled(library_ready and item is not None and not item.archived)
        self.library_restore_button.setEnabled(library_ready and item is not None and item.archived)
        self.library_delete_button.setEnabled(library_ready and item is not None)
        self.library_retry_button.setVisible(self._library_indexing_degraded)
        self.library_retry_button.setEnabled(library_ready and self._library_indexing_degraded)

    def _selected_library_item(self) -> DesktopMemoryItem | None:
        row = self.library_table.currentRow()
        return self._library_items[row] if self.library_table.selectedItems() and 0 <= row < len(self._library_items) else None

    @Slot()
    def _library_selection_changed(self) -> None:
        item = self._selected_library_item()
        self.library_content.setPlainText(item.content if item else "")
        self._update_controls()

    @Slot()
    def _refresh_library(self, *, preserve_status: bool = False) -> None:
        if not self._rag_ready or self._busy or self._recording or self._voice_question_active or self._save_pending or self._closing:
            return
        item = self._selected_library_item()
        self._library_selection_id = item.id if item else None
        self._busy = self._library_active = True
        if not preserve_status:
            self.library_status.setText("Loading memories...")
        self.library_content.clear()
        self._update_controls()
        self._worker.request_library(DesktopMemoryFilter(self.library_filter.currentData()), limit=100)

    @Slot(object)
    def _on_library_loaded(self, items: tuple[DesktopMemoryItem, ...]) -> None:
        if self._closing:
            return
        self._library_items = items
        self.library_table.blockSignals(True)
        self.library_table.clearSelection()
        self.library_table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = (item.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"), str(item.revision),
                      "Archived" if item.archived else "Active", " ".join(item.content.split())[:160])
            for column, value in enumerate(values):
                self.library_table.setItem(row, column, QTableWidgetItem(value))
            if item.id == self._library_selection_id:
                self.library_table.selectRow(row)
        self.library_table.blockSignals(False)
        if self._library_active:
            self._busy = self._library_active = False
        if self.library_status.text() in {"Starting...", "Loading memories..."}:
            self.library_status.setText(f"{len(items)} memories (limit 100)")
        self._library_selection_changed()

    def _begin_library_action(self, message: str) -> None:
        self._busy = self._library_active = True
        self.library_status.setText(message)
        self._update_controls()

    @Slot()
    def _edit_library_memory(self) -> None:
        item = self._selected_library_item()
        if not self.library_edit_button.isEnabled() or item is None:
            return
        self._begin_library_action("Editing memory...")
        dialog = MemoryEditDialog(item, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if self._closing:
            return
        if accepted:
            self._worker.request_edit_memory(item, dialog.content)
        else:
            self._busy = self._library_active = False
            self.library_status.setText("Edit cancelled")
            self._update_controls()

    @Slot()
    def _archive_library_memory(self) -> None:
        item = self._selected_library_item()
        if not self.library_archive_button.isEnabled() or item is None:
            return
        self._begin_library_action("Archiving memory...")
        confirmed = QMessageBox.question(self, "Archive Memory", "Archive this memory?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if self._closing:
            return
        if confirmed == QMessageBox.StandardButton.Yes:
            self._worker.request_archive_memory(item.id)
        else:
            self._busy = self._library_active = False
            self.library_status.setText("Archive cancelled")
            self._update_controls()

    @Slot()
    def _restore_library_memory(self) -> None:
        item = self._selected_library_item()
        if self.library_restore_button.isEnabled() and item is not None:
            self._begin_library_action("Restoring memory...")
            self._worker.request_restore_memory(item.id)

    @Slot()
    def _delete_library_memory(self) -> None:
        item = self._selected_library_item()
        if not self.library_delete_button.isEnabled() or item is None:
            return
        self._begin_library_action("Confirm permanent deletion...")
        accepted = MemoryDeleteDialog(item, self).exec() == QDialog.DialogCode.Accepted
        if self._closing:
            return
        output = ""
        if accepted:
            suggested = Path.home() / f"kulai_memory_before_delete_{datetime.now():%Y%m%d_%H%M%S}.dump"
            output, _ = QFileDialog.getSaveFileName(self, "Choose a NEW backup outside the repository",
                str(suggested), "PostgreSQL backup (*.dump)",
                options=QFileDialog.Option.DontConfirmOverwrite)
        if self._closing:
            return
        if not accepted or not output:
            self._busy = self._library_active = False
            self.library_status.setText("Delete cancelled")
            self._update_controls()
            return
        self.library_status.setText("Preparing verified backup...")
        self._worker.request_delete_memory(item, Path(output))

    @Slot(object)
    def _on_memory_deleted(self, result: DesktopMemoryDeleteResult) -> None:
        if self._closing:
            return
        self._busy = self._library_active = False
        self._library_selection_id = None
        self.library_content.clear()
        self._on_library_loaded(tuple(item for item in self._library_items if item.id != result.memory_id))
        self.library_status.setText(f"Memory deleted. Retain backup: {result.backup_path}")
        self._refresh_library(preserve_status=True)

    @Slot(object)
    def _on_library_changed(self, result: DesktopMemoryChangeResult) -> None:
        if self._closing:
            return
        self._busy = self._library_active = False
        self._library_selection_id = result.item.id
        current_archived = DesktopMemoryFilter(self.library_filter.currentData()) is DesktopMemoryFilter.ARCHIVED
        items = tuple(result.item if item.id == result.item.id else item for item in self._library_items)
        if result.item.archived != current_archived:
            items = tuple(item for item in items if item.id != result.item.id)
        self._on_library_loaded(items)
        if result.indexing_degraded:
            self._library_indexing_degraded = True
            self.library_status.setText("Memory was updated, but semantic indexing needs retry." if
                result.status is DesktopMemoryChangeStatus.EDITED else "Memory was restored, but semantic indexing needs retry.")
        else:
            self.library_status.setText({DesktopMemoryChangeStatus.EDITED: "Memory updated",
                DesktopMemoryChangeStatus.UNCHANGED: "Memory unchanged", DesktopMemoryChangeStatus.ARCHIVED: "Memory archived",
                DesktopMemoryChangeStatus.ALREADY_ARCHIVED: "Memory already archived", DesktopMemoryChangeStatus.RESTORED: "Memory restored",
                DesktopMemoryChangeStatus.ALREADY_ACTIVE: "Memory already active"}[result.status])
        self._refresh_library(preserve_status=True)

    @Slot()
    def _retry_library_indexing(self) -> None:
        if self.library_retry_button.isEnabled():
            self._begin_library_action("Retrying semantic indexing...")
            self._worker.request_library_indexing_retry()

    @Slot(object)
    def _on_library_indexing_finished(self, report) -> None:
        if self._closing:
            return
        self._busy = self._library_active = False
        self._library_indexing_degraded = report.degraded
        self.library_status.setText("Semantic indexing needs retry." if report.degraded else "Semantic indexing is current")
        self._refresh_library(preserve_status=True)

    @Slot()
    def _ask_memory(self) -> None:
        if not self.ask_button.isEnabled():
            return
        self._busy = self._rag_active = True
        self.answer.clear()
        self.sources_table.setRowCount(0)
        self.rag_status.setText("Searching memory...")
        self._update_controls()
        self._worker.request_ask_memory(self.query_input.text(), top_k=5)

    @Slot()
    def _toggle_voice_question(self) -> None:
        if not self.voice_ask_button.isEnabled():
            return
        if self._question_recording:
            self._question_recording = False
            self._busy = True
            self._question_timer.stop()
            self.rag_status.setText("Transcribing question...")
            self._update_controls()
            self._worker.request_stop_voice_question_and_ask(top_k=5)
            return
        device_id = self.device_selector.currentData()
        if device_id is None:
            return
        self._busy = self._voice_question_active = self._rag_active = True
        self.answer.clear()
        self.sources_table.setRowCount(0)
        self.query_input.clear()
        self.rag_status.setText("Opening microphone...")
        self._update_controls()
        self._worker.request_start_voice_question(int(device_id))

    @Slot()
    def _on_voice_question_started(self) -> None:
        if self._closing or not self._voice_question_active:
            return
        self._busy = False
        self._question_recording = True
        self._question_timer.start(round(DEFAULT_MAX_DURATION_SECONDS * 1000))
        self.rag_status.setText("Listening...")
        self._update_controls()

    @Slot(object)
    def _on_voice_question_finished(self, result: DesktopVoiceQuestionResult) -> None:
        if self._closing or not self._voice_question_active:
            return
        self._voice_question_active = self._question_recording = False
        self._question_timer.stop()
        self.query_input.setText(result.transcript)
        if result.rag_result is not None:
            self._on_rag_finished(result.rag_result)
        else:
            self._busy = self._rag_active = False
            self.answer.clear()
            self.sources_table.setRowCount(0)
            self.rag_status.setText("No speech detected")
            self._update_controls()

    @Slot(object)
    def _on_rag_finished(self, result: DesktopRagResult) -> None:
        if self._closing or not self._rag_active:
            return
        self._busy = self._rag_active = False
        self.answer.setPlainText(result.answer)
        self.sources_table.setRowCount(0)
        if result.status is DesktopRagStatus.ANSWERED:
            self.rag_status.setText("Answered")
            self.sources_table.setRowCount(len(result.citations))
            for row, citation in enumerate(result.citations):
                for column, value in enumerate((str(citation.rank), str(citation.memory_id), f"{citation.score:.4f}")):
                    self.sources_table.setItem(row, column, QTableWidgetItem(value))
            self.sources_table.resizeColumnToContents(0)
            self.sources_table.resizeColumnToContents(1)
        elif result.status is DesktopRagStatus.INSUFFICIENT_CONTEXT:
            self.rag_status.setText("Not enough information")
        else:
            self.answer.clear()
            self.rag_status.setText(result.error_message or "An answer could not be generated.")
        self._update_controls()

    @Slot(object)
    def _on_startup_ready(self, result: DesktopStartupResult) -> None:
        if self._closing:
            return
        self.device_selector.clear()
        default_index = 0
        for index, device in enumerate(result.devices):
            self.device_selector.addItem(device.display_name, device.device_id)
            if device.is_default:
                default_index = index
        if result.devices:
            self.device_selector.setCurrentIndex(default_index)
        self._set_recent(result.memories)
        self._ready = bool(result.devices)
        self._rag_ready = True
        self._library_indexing_degraded = result.indexing.degraded
        self.rag_status.setText("Ready")
        self.library_status.setText("Click REFRESH to load memories (limit 100)")
        self.status_label.setText("Ready" if result.devices else "No input device found")
        if result.indexing.degraded:
            self.save_status.setText("Semantic indexing is degraded; missing indexes can be retried.")
        self.record_button.setEnabled(self._ready)
        self.refresh_button.setEnabled(True)
        self._update_controls()
        if self._worker.isRunning():
            self._refresh_library()

    @Slot(str)
    def _on_startup_failed(self, message: str) -> None:
        if self._closing:
            return
        self.status_label.setText(message)
        self.save_status.setText("Startup failed")
        self._ready = False
        self._rag_ready = False
        self.rag_status.setText(message)
        self.record_button.setEnabled(False)
        self._update_controls()

    @Slot()
    def _start_recording(self) -> None:
        if (self._busy or self._recording or self._voice_question_active
                or self._save_pending or self._closing):
            return
        device_id = self.device_selector.currentData()
        if device_id is None:
            return
        self._busy = True
        self._update_controls()
        self.record_button.setEnabled(False)
        self.device_selector.setEnabled(False)
        self.retry_button.setVisible(False)
        self.save_status.clear()
        self.transcript.clear()
        self.status_label.setText("Opening microphone...")
        self._worker.request_start_recording(int(device_id))

    @Slot(object)
    def _on_recording_started(self, ingestion_id: object) -> None:
        if self._closing:
            return
        del ingestion_id
        self._recording = True
        self._busy = False
        self._update_controls()
        self._elapsed.start()
        self._elapsed_timer.start()
        self.elapsed_label.setText("00:00")
        self.status_label.setText("Recording...")
        self.stop_button.setEnabled(True)

    @Slot()
    def _stop_recording(self) -> None:
        if not self._recording or self._closing:
            return
        self._recording = False
        self._busy = True
        self._update_controls()
        self._elapsed_timer.stop()
        self.stop_button.setEnabled(False)
        self.status_label.setText("Transcribing...")
        self._worker.request_stop_and_process()

    @Slot(object)
    def _on_progress(self, progress: DesktopProgress | DesktopRagProgress | DesktopVoiceQuestionProgress | DesktopDeleteProgress) -> None:
        if self._closing:
            return
        if isinstance(progress, DesktopDeleteProgress):
            if self._library_active:
                self.library_status.setText({DesktopDeleteProgressState.PREPARING_BACKUP: "Preparing verified backup...",
                    DesktopDeleteProgressState.VERIFYING_BACKUP: "Verifying backup restore...",
                    DesktopDeleteProgressState.DELETING: "Deleting memory..."}[progress.state])
            return
        if isinstance(progress, DesktopVoiceQuestionProgress):
            if self._voice_question_active:
                if progress.state is DesktopVoiceQuestionProgressState.TRANSCRIBING:
                    self.rag_status.setText("Transcribing question...")
                elif progress.state is DesktopVoiceQuestionProgressState.TRANSCRIPT_READY:
                    self.query_input.setText(progress.transcript or "")
            return
        if isinstance(progress, DesktopRagProgress):
            if self._rag_active:
                self.rag_status.setText("Searching memory..." if progress.state is DesktopRagProgressState.RETRIEVING
                                        else "Generating answer...")
            return
        if progress.state is DesktopProgressState.TRANSCRIBING:
            self.status_label.setText("Transcribing...")
        elif progress.state is DesktopProgressState.TRANSCRIPT_READY:
            self.transcript.setPlainText(progress.transcript or "")
        elif progress.state is DesktopProgressState.SAVING:
            self.status_label.setText("Saving...")
        elif progress.state is DesktopProgressState.INDEXING:
            self.status_label.setText("Indexing...")

    @Slot(object)
    def _on_processing_finished(self, result: DesktopProcessingResult) -> None:
        if self._closing:
            return
        self._busy = False
        self._save_pending = result.save_pending
        self.transcript.setPlainText(result.transcript)
        self.retry_button.setVisible(result.save_pending)
        self.retry_button.setEnabled(result.save_pending)
        self.retry_button.setText(
            "RETRY INDEXING" if result.status is DesktopResultStatus.INDEXING_FAILED else "RETRY SAVE"
        )
        self.device_selector.setEnabled(not result.save_pending)

        if result.status is DesktopResultStatus.CREATED:
            self.status_label.setText("Saved")
            self.save_status.setText("CREATED")
            self._worker.request_recent()
        elif result.status is DesktopResultStatus.DUPLICATE:
            self.status_label.setText("Already saved")
            self.save_status.setText("DUPLICATE")
            self._worker.request_recent()
        elif result.status is DesktopResultStatus.SKIPPED_EMPTY:
            self.status_label.setText("No speech detected")
            self.save_status.setText("SKIPPED_EMPTY")
        elif result.status is DesktopResultStatus.INGESTION_RETIRED:
            self.status_label.setText("This note was deleted and cannot be saved again.")
            self.save_status.setText("INGESTION_RETIRED")
        elif result.status is DesktopResultStatus.INGESTION_ARCHIVED:
            self.status_label.setText("This memory is archived; retry cannot restore it.")
            self.save_status.setText("INGESTION_ARCHIVED")
        elif result.status is DesktopResultStatus.INGESTION_CONFLICT:
            self.status_label.setText("This note conflicts with the current memory content.")
            self.save_status.setText("INGESTION_CONFLICT")
        elif result.status is DesktopResultStatus.INDEXING_FAILED:
            self.status_label.setText("Memory saved; semantic indexing failed - retry available")
            self.save_status.setText("INDEXING_FAILED")
            self._worker.request_recent()
        else:
            self.status_label.setText("Save failed - retry available")
            self.save_status.setText("SAVE_FAILED")

        self.record_button.setEnabled(self._ready and not result.save_pending)
        self._update_controls()

    @Slot()
    def _retry_save(self) -> None:
        if self._busy or self._voice_question_active or self._closing:
            return
        self._busy = True
        self._update_controls()
        self.retry_button.setEnabled(False)
        self.status_label.setText("Saving...")
        self._worker.request_retry_save()

    @Slot(object)
    def _set_recent(self, memories: tuple[MemorySummary, ...]) -> None:
        if self._closing:
            return
        self.recent_table.setRowCount(len(memories))
        for row, memory in enumerate(memories):
            created = memory.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            created_item = QTableWidgetItem(created)
            content_item = QTableWidgetItem(memory.content)
            content_item.setToolTip(memory.content)
            self.recent_table.setItem(row, 0, created_item)
            self.recent_table.setItem(row, 1, content_item)
        self.recent_table.resizeColumnToContents(0)

    @Slot(str, str)
    def _on_operation_failed(self, operation: str, message: str) -> None:
        if self._closing:
            return
        self._busy = False
        if operation.startswith("library_"):
            self._library_active = False
            self.library_status.setText(message)
            if operation == "library_read":
                self._library_items = ()
                self.library_table.setRowCount(0)
                self.library_content.clear()
            self._update_controls()
            if message in {"Memory changed. Reload it before editing.", "Memory changed. Reload it before deleting.",
                           "Archived memories cannot be edited."}:
                self._refresh_library(preserve_status=True)
            return
        if operation in {"ask", "voice_question_start", "voice_question"}:
            self._rag_active = False
            self._voice_question_active = self._question_recording = False
            self._question_timer.stop()
            self.answer.clear()
            self.sources_table.setRowCount(0)
            self.rag_status.setText(message)
            self._update_controls()
            return
        if operation == "record":
            self.status_label.setText("Microphone unavailable")
            self.device_selector.setEnabled(True)
            self.record_button.setEnabled(self._ready)
        elif operation == "process":
            self._recording = False
            self._elapsed_timer.stop()
            self.status_label.setText("Transcription failed")
            self.device_selector.setEnabled(True)
            self.record_button.setEnabled(self._ready)
        elif operation == "retry_save":
            self.status_label.setText("Save failed - retry available")
            self.retry_button.setEnabled(True)
        else:
            self.status_label.setText(message)
        self.save_status.setText(message)
        self._update_controls()

    @Slot()
    def _update_elapsed(self) -> None:
        if self._closing:
            return
        elapsed_ms = self._elapsed.elapsed()
        seconds = max(0, elapsed_ms // 1000)
        self.elapsed_label.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")
        if elapsed_ms >= round(DEFAULT_MAX_DURATION_SECONDS * 1000):
            self._stop_recording()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_done or not self._worker.isRunning():
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self._elapsed_timer.stop()
            self._question_timer.stop()
            self.status_label.setText("Closing...")
            self.record_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.retry_button.setEnabled(False)
            self._update_controls()
            self._worker.request_shutdown()

    @Slot()
    def _on_shutdown_finished(self) -> None:
        if not self._closing:
            return
        self._shutdown_done = True
        if not self._worker.isRunning():
            self.close()


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
