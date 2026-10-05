from __future__ import annotations

import asyncio
import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402

from kulai_memory.desktop.app import (  # noqa: E402
    AsyncDesktopThread,
    MainWindow,
    _public_message,
)
from kulai_memory.desktop.models import (  # noqa: E402
    DesktopConfigurationError,
    DesktopDatabaseError,
    DesktopPublicError,
    DesktopRecordingError,
    DesktopStartupResult,
    MicrophoneDevice,
)


def test_main_window_can_be_created_and_closed_offscreen() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(autostart=False)

    assert window.windowTitle() == "KulAI Memory"
    assert window.record_button.text() == "NAGRAJ"
    assert window.stop_button.text() == "STOP"
    assert window.retry_button.text() == "RETRY SAVE"
    assert window.retry_button.isVisible() is False

    window.show()
    app.processEvents()
    window.close()
    app.processEvents()
    assert window.isVisible() is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DesktopDatabaseError(), DesktopDatabaseError.safe_message),
        (DesktopRecordingError(), DesktopRecordingError.safe_message),
        (DesktopConfigurationError(), DesktopConfigurationError.safe_message),
        (RuntimeError("private detail"), DesktopPublicError.safe_message),
    ],
)
def test_startup_failure_uses_safe_error_specific_message(
    error: Exception,
    expected: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(autostart=False)

    message = _public_message(error)
    window._on_startup_failed(message)

    assert message == expected
    assert window.status_label.text() == expected
    assert window.save_status.text() == "Startup failed"
    if not isinstance(error, DesktopDatabaseError):
        assert "Database" not in window.status_label.text()
    window.close()
    app.processEvents()


def test_worker_starts_runtime_on_dedicated_asyncio_thread() -> None:
    app = QApplication.instance() or QApplication([])

    class FakeController:
        def __init__(self, *, progress_callback) -> None:
            self.progress_callback = progress_callback

        async def startup(self) -> DesktopStartupResult:
            await asyncio.sleep(0.01)
            return DesktopStartupResult(
                devices=(MicrophoneDevice(7, "Fake mic", None, True),),
                memories=(),
            )

        async def shutdown(self) -> None:
            await asyncio.sleep(0)

    worker = AsyncDesktopThread(controller_factory=FakeController)
    startup_spy = QSignalSpy(worker.startup_ready)
    shutdown_spy = QSignalSpy(worker.shutdown_finished)
    window = MainWindow(worker=worker)

    for _ in range(200):
        if startup_spy.count():
            break
        QTest.qWait(10)
    assert startup_spy.count() == 1
    for _ in range(200):
        if window.status_label.text() == "Ready":
            break
        QTest.qWait(10)
    assert window.status_label.text() == "Ready"
    assert window.record_button.isEnabled()

    window.close()
    for _ in range(200):
        if shutdown_spy.count():
            break
        QTest.qWait(10)
    assert shutdown_spy.count() == 1
    assert worker.wait(2_000)
    app.processEvents()


def test_window_can_close_while_runtime_is_still_starting() -> None:
    app = QApplication.instance() or QApplication([])

    class SlowController:
        def __init__(self, *, progress_callback) -> None:
            self.progress_callback = progress_callback

        async def startup(self) -> DesktopStartupResult:
            await asyncio.sleep(0.05)
            return DesktopStartupResult(devices=(), memories=())

        async def shutdown(self) -> None:
            await asyncio.sleep(0)

    worker = AsyncDesktopThread(controller_factory=SlowController)
    shutdown_spy = QSignalSpy(worker.shutdown_finished)
    window = MainWindow(worker=worker)

    window.close()
    for _ in range(200):
        if shutdown_spy.count():
            break
        QTest.qWait(10)

    assert shutdown_spy.count() == 1
    assert worker.wait(2_000)
    app.processEvents()
