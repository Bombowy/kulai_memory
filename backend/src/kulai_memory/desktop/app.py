"""PySide6 desktop window and its dedicated asyncio worker thread."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QElapsedTimer, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
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
    recent_loaded = Signal(object)
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

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        controller: DesktopController | None = None
        try:
            try:
                controller = self._controller_factory(
                    progress_callback=self.progress.emit
                )
                self._controller = controller
                startup = loop.run_until_complete(controller.startup())
            except Exception as exc:
                self.startup_failed.emit(_public_message(exc))
                if controller is not None:
                    loop.run_until_complete(controller.shutdown())
                if self._shutdown_requested.is_set():
                    self.shutdown_finished.emit()
                return
            if self._shutdown_requested.is_set():
                loop.run_until_complete(controller.shutdown())
                self.shutdown_finished.emit()
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
            self.shutdown_finished.emit()
            loop = asyncio.get_running_loop()
            loop.call_soon(loop.stop)

    def _submit(
        self,
        operation: str,
        create: Callable[[DesktopController], Coroutine[Any, Any, Any]],
        success: Callable[[object], None],
    ) -> None:
        loop = self._loop
        controller = self._controller
        if loop is None or controller is None or loop.is_closed():
            self.operation_failed.emit(operation, "Desktop runtime is not ready.")
            return
        asyncio.run_coroutine_threadsafe(
            self._execute(operation, create(controller), success),
            loop,
        )

    async def _execute(
        self,
        operation: str,
        coroutine: Coroutine[Any, Any, Any],
        success: Callable[[object], None],
    ) -> None:
        try:
            result = await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.operation_failed.emit(operation, _public_message(exc))
        else:
            success(result)


def _public_message(exc: Exception) -> str:
    if isinstance(exc, DesktopPublicError):
        return exc.public_message
    return "The desktop operation could not be completed."


class MainWindow(QMainWindow):
    """Minimal usable voice-memory desktop window."""

    def __init__(
        self,
        *,
        worker: AsyncDesktopThread | None = None,
        autostart: bool = True,
    ) -> None:
        super().__init__()
        self.setWindowTitle("KulAI Memory")
        self.resize(820, 650)
        self._worker = worker or AsyncDesktopThread(parent=self)
        self._recording = False
        self._ready = False
        self._closing = False
        self._shutdown_done = False
        self._elapsed = QElapsedTimer()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

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
        self.setCentralWidget(root)

        self.record_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.record_button.clicked.connect(self._start_recording)
        self.stop_button.clicked.connect(self._stop_recording)
        self.retry_button.clicked.connect(self._retry_save)
        self.refresh_button.clicked.connect(self._worker.request_recent)

    def _connect_worker(self) -> None:
        self._worker.startup_ready.connect(self._on_startup_ready)
        self._worker.startup_failed.connect(self._on_startup_failed)
        self._worker.recording_started.connect(self._on_recording_started)
        self._worker.progress.connect(self._on_progress)
        self._worker.processing_finished.connect(self._on_processing_finished)
        self._worker.recent_loaded.connect(self._set_recent)
        self._worker.operation_failed.connect(self._on_operation_failed)
        self._worker.shutdown_finished.connect(self._on_shutdown_finished)

    @Slot(object)
    def _on_startup_ready(self, result: DesktopStartupResult) -> None:
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
        self.status_label.setText("Ready" if result.devices else "No input device found")
        self.record_button.setEnabled(self._ready)
        self.refresh_button.setEnabled(True)

    @Slot(str)
    def _on_startup_failed(self, message: str) -> None:
        self.status_label.setText(message)
        self.save_status.setText("Startup failed")
        self._ready = False
        self.record_button.setEnabled(False)

    @Slot()
    def _start_recording(self) -> None:
        device_id = self.device_selector.currentData()
        if device_id is None:
            return
        self.record_button.setEnabled(False)
        self.device_selector.setEnabled(False)
        self.retry_button.setVisible(False)
        self.save_status.clear()
        self.transcript.clear()
        self.status_label.setText("Opening microphone...")
        self._worker.request_start_recording(int(device_id))

    @Slot(object)
    def _on_recording_started(self, ingestion_id: object) -> None:
        del ingestion_id
        self._recording = True
        self._elapsed.start()
        self._elapsed_timer.start()
        self.elapsed_label.setText("00:00")
        self.status_label.setText("Recording...")
        self.stop_button.setEnabled(True)

    @Slot()
    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._elapsed_timer.stop()
        self.stop_button.setEnabled(False)
        self.status_label.setText("Transcribing...")
        self._worker.request_stop_and_process()

    @Slot(object)
    def _on_progress(self, progress: DesktopProgress) -> None:
        if progress.state is DesktopProgressState.TRANSCRIBING:
            self.status_label.setText("Transcribing...")
        elif progress.state is DesktopProgressState.TRANSCRIPT_READY:
            self.transcript.setPlainText(progress.transcript or "")
        elif progress.state is DesktopProgressState.SAVING:
            self.status_label.setText("Saving...")

    @Slot(object)
    def _on_processing_finished(self, result: DesktopProcessingResult) -> None:
        self.transcript.setPlainText(result.transcript)
        self.retry_button.setVisible(result.save_pending)
        self.retry_button.setEnabled(result.save_pending)
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
        else:
            self.status_label.setText("Save failed - retry available")
            self.save_status.setText("SAVE_FAILED")

        self.record_button.setEnabled(self._ready and not result.save_pending)

    @Slot()
    def _retry_save(self) -> None:
        self.retry_button.setEnabled(False)
        self.status_label.setText("Saving...")
        self._worker.request_retry_save()

    @Slot(object)
    def _set_recent(self, memories: tuple[MemorySummary, ...]) -> None:
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

    @Slot()
    def _update_elapsed(self) -> None:
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
            self.status_label.setText("Closing...")
            self.record_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.retry_button.setEnabled(False)
            self._worker.request_shutdown()

    @Slot()
    def _on_shutdown_finished(self) -> None:
        self._shutdown_done = True
        self.close()


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
