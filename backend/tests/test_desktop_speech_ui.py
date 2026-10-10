from __future__ import annotations

import asyncio
import os
import threading

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from backend.tests.test_desktop_rag_ui import ready_window, wait_until
from backend.tests.test_desktop_speech import setup
from kulai_memory.desktop.app import AsyncDesktopThread, MainWindow
from kulai_memory.desktop.models import (
    DesktopSpeechProgress, DesktopSpeechProgressState, DesktopVoiceQuestionResult,
)


def speech_window(monkeypatch):
    app, window, asked, recent = ready_window(monkeypatch)
    window._tts_available = True
    window._update_controls()
    spoken, stopped = [], []
    monkeypatch.setattr(window._worker, 'request_speak_answer', lambda result: spoken.append(result))
    monkeypatch.setattr(window._worker, 'request_stop_audio', lambda: stopped.append(1))
    return app, window, asked, recent, spoken, stopped


def show_answer(window):
    from uuid import UUID
    from kulai_memory.desktop.models import DesktopRagResult, DesktopRagStatus, DesktopRagCitation
    result = DesktopRagResult(DesktopRagStatus.ANSWERED, 'Synthetic answer',
                              (DesktopRagCitation(UUID(int=9), 1, .9),))
    window._rag_active = True
    window._on_rag_finished(result)
    return result


def test_widgets_states_stop_finish_and_secondary_failure_keep_answer(monkeypatch):
    app, w, _, _, spoken, stopped = speech_window(monkeypatch)
    try:
        assert w.speak_button.text() == 'SPEAK' and w.stop_audio_button.text() == 'STOP AUDIO'
        assert w.auto_speak_checkbox.isChecked()
        assert w.auto_speak_checkbox.text() == 'Speak answers automatically'
        assert not w.speak_button.isEnabled() and not w.stop_audio_button.isEnabled()
        w.auto_speak_checkbox.setChecked(False)
        result = show_answer(w)
        assert w.speak_button.isEnabled() and not spoken
        w.speak_button.click()
        assert spoken == [result] and w.speech_status.text() == 'Preparing speech...'
        assert not w.record_button.isEnabled() and not w.library_refresh_button.isEnabled()
        assert w.stop_audio_button.isEnabled()
        w._on_progress(DesktopSpeechProgress(DesktopSpeechProgressState.SYNTHESIZING))
        assert w.speech_status.text() == 'Synthesizing speech...'
        w._on_progress(DesktopSpeechProgress(DesktopSpeechProgressState.SPEAKING))
        assert w.speech_status.text() == 'Speaking...' and w.record_button.isEnabled()
        w.stop_audio_button.click()
        assert stopped == [1] and w.speech_status.text() == 'Stopping speech...'
        assert not w.speak_button.isEnabled() and not w.stop_audio_button.isEnabled()
        # Queued progress must not undo STOP's disabled state/status.
        w._on_progress(DesktopSpeechProgress(DesktopSpeechProgressState.SPEAKING))
        assert w.speech_status.text() == 'Stopping speech...' and not w.stop_audio_button.isEnabled()
        w._on_speech_stopped()
        assert w.speech_status.text() == 'Speech stopped' and w.speak_button.isEnabled()
        for outcome in ('finish', 'failure'):
            w.speak_button.click()
            if outcome == 'finish':
                w._on_progress(DesktopSpeechProgress(DesktopSpeechProgressState.FINISHED))
                assert w.speech_status.text() == 'Speech finished'
            else:
                w._on_operation_failed('speech', 'Could not speak answer.')
                assert w.speech_status.text() == 'Could not speak answer.'
            assert w.answer.toPlainText() == result.answer and w.sources_table.rowCount() == 1
            assert w.rag_status.text() == 'Answered' and w.speak_button.isEnabled()
            assert w.library_refresh_button.isEnabled() and w.record_button.isEnabled()
    finally:
        w.close()
        app.processEvents()


@pytest.mark.parametrize('enabled', [False, True])
def test_voice_auto_speak_checkbox_uses_rendered_validated_result(monkeypatch, enabled):
    app, w, _, _, spoken, _ = speech_window(monkeypatch)
    try:
        w.auto_speak_checkbox.setChecked(False)
        result = show_answer(w)
        w.auto_speak_checkbox.setChecked(enabled)
        w._rag_active = w._voice_question_active = True
        w._on_voice_question_finished(DesktopVoiceQuestionResult('controlled question', result))
        assert spoken == ([result] if enabled else [])
        assert w.answer.toPlainText() == result.answer and w.sources_table.rowCount() == 1
        assert w.query_input.text() == 'controlled question'
        if enabled:
            w._on_operation_failed('speech', 'Could not speak answer.')
        assert w.rag_status.text() == 'Answered'
    finally:
        w.close()
        app.processEvents()


@pytest.mark.parametrize('operation', ['ask', 'voice_question', 'note'])
def test_new_request_stops_existing_playback_first(monkeypatch, operation):
    app, w, _, _, spoken, stopped = speech_window(monkeypatch)
    calls = []
    monkeypatch.setattr(w._worker, 'request_stop_audio', lambda: calls.append('stop'))
    monkeypatch.setattr(w._worker, 'request_ask_memory', lambda *a, **kw: calls.append('ask'))
    monkeypatch.setattr(w._worker, 'request_start_voice_question', lambda *a: calls.append('voice_question'))
    monkeypatch.setattr(w._worker, 'request_start_recording', lambda *a: calls.append('note'))
    try:
        w.auto_speak_checkbox.setChecked(False)
        show_answer(w)
        w.query_input.setText('controlled question')
        w.speak_button.click()
        w._on_progress(DesktopSpeechProgress(DesktopSpeechProgressState.SPEAKING))
        {'ask': w.ask_button, 'voice_question': w.voice_ask_button, 'note': w.record_button}[operation].click()
        assert calls == [operation]  # Its controller coroutine owns stop-and-drain.
        assert not w._speech_active and w.speech_status.text() == 'Stopping speech...'
    finally:
        w.close()
        app.processEvents()


def test_unavailable_keeps_rag_library_and_voice_ready(monkeypatch):
    app, w, _, _ = ready_window(monkeypatch)
    try:
        show_answer(w)
        assert not w.speak_button.isEnabled() and not w.auto_speak_checkbox.isEnabled()
        assert w.speech_status.text() == 'TTS unavailable.'
        assert w.record_button.isEnabled() and w.library_refresh_button.isEnabled()
    finally:
        w.close()
        app.processEvents()


@pytest.mark.parametrize('phase', ['planner', 'synthesis', 'playback'])
def test_real_worker_close_cancels_speech_keeps_qt_responsive_and_no_stale_signals(tmp_path, phase):
    app = QApplication.instance() or QApplication([])
    h, c, _, provider, player, runtime, result = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    errors, threads = [], []
    if phase == 'planner':
        async def block(**kw):
            entered.set()
            threads.append(threading.get_ident())
            await asyncio.sleep(180)
        h.rag.plan_speech = block
    elif phase == 'synthesis':
        from kulai_memory.local_tts import drain_audio_work
        original = provider.synthesize
        async def synthesize(segment):
            def native():
                entered.set()
                threads.append(threading.get_ident())
                assert release.wait(5)
            await drain_audio_work(asyncio.create_task(asyncio.to_thread(native)))
            return await original(segment)
        provider.synthesize = synthesize
    else:
        original = player.play
        async def play(path):
            entered.set()
            threads.append(threading.get_ident())
            await original(path)
        player.play = play
        player.release.clear()
    def factory(*, progress_callback):
        asyncio.get_event_loop().set_exception_handler(lambda loop, context: errors.append(context))
        c._progress_callback = progress_callback
        return c
    worker = AsyncDesktopThread(controller_factory=factory)
    failed, stopped = QSignalSpy(worker.operation_failed), QSignalSpy(worker.shutdown_finished)
    window = MainWindow(worker=worker)
    timer, ticks = QTimer(), []
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    try:
        window.show()
        wait_until(lambda: window._tts_available)
        window._rag_active = True
        window._on_rag_finished(result)
        window.speak_button.click()
        wait_until(entered.is_set)
        count = len(ticks)
        QTest.qWait(40)
        assert len(ticks) > count and all(t != threading.get_ident() for t in threads)
        paths = runtime.artifacts
        window.close()
        if phase == 'synthesis':
            QTest.qWait(30)
            assert worker.isRunning() and provider.closed == 0
            release.set()
        wait_until(lambda: not worker.isRunning())
        assert stopped.count() == 1 and failed.count() == 0 and not errors
        assert provider.closed == h.embedding.closed == h.rag.closed == 1
        assert all(not path.exists() for path in paths) and not runtime.artifacts
        assert worker._loop is None and worker._controller is None
        app.processEvents()
        assert not window.isVisible()
    finally:
        timer.stop()
        release.set()
        worker.request_shutdown()
        assert worker.wait(2000)
        window.close()
