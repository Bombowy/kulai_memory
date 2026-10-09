from __future__ import annotations

import asyncio
import os
import threading
from uuid import UUID

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from backend.tests.test_desktop_rag_ui import ready_window, wait_until
from backend.tests.test_desktop_voice_question import harness, QUERY
from kulai_memory.desktop.app import AsyncDesktopThread, MainWindow
from kulai_memory.desktop.models import (
    DesktopRagCitation, DesktopRagProgress, DesktopRagProgressState, DesktopRagResult,
    DesktopRagStatus, DesktopVoiceQuestionProgress, DesktopVoiceQuestionProgressState,
    DesktopVoiceQuestionResult,
)


def begin(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window._worker, 'request_start_voice_question', lambda device: calls.append(('start', device)))
    monkeypatch.setattr(window._worker, 'request_stop_voice_question_and_ask', lambda top_k: calls.append(('stop', top_k)))
    window.voice_ask_button.click()
    window._on_voice_question_started()
    return calls


def test_voice_question_button_states_transcript_and_existing_answer_rendering(monkeypatch):
    app, window, _, recent = ready_window(monkeypatch)
    window.transcript.setPlainText('previous Voice Note')
    assert window.voice_ask_button.text() == 'ASK BY VOICE' and window.voice_ask_button.isEnabled()
    calls = begin(window, monkeypatch)
    assert calls == [('start', 3)] and window.voice_ask_button.text() == 'STOP QUESTION'
    assert window.rag_status.text() == 'Listening...' and window._question_timer.isActive()
    assert not window.ask_button.isEnabled() and not window.record_button.isEnabled()
    assert not window.stop_button.isEnabled() and not window.refresh_button.isEnabled()
    window.voice_ask_button.click()
    assert calls == [('start', 3), ('stop', 5)]
    assert window.rag_status.text() == 'Transcribing question...' and not window.voice_ask_button.isEnabled()
    window._on_progress(DesktopVoiceQuestionProgress(DesktopVoiceQuestionProgressState.TRANSCRIPT_READY, QUERY))
    assert window.query_input.text() == QUERY
    for state, message in ((DesktopRagProgressState.RETRIEVING, 'Searching memory...'),
                           (DesktopRagProgressState.GENERATING, 'Generating answer...')):
        window._on_progress(DesktopRagProgress(state))
        assert window.rag_status.text() == message
    rag = DesktopRagResult(DesktopRagStatus.ANSWERED, 'Wenus', (DesktopRagCitation(UUID(int=1), 2, 0.98765),))
    window._on_voice_question_finished(DesktopVoiceQuestionResult(QUERY, rag))
    assert window.rag_status.text() == 'Answered' and window.answer.toPlainText() == 'Wenus'
    assert [window.sources_table.item(0, i).text() for i in range(3)] == ['2', str(UUID(int=1)), '0.9877']
    assert window.voice_ask_button.text() == 'ASK BY VOICE' and window.voice_ask_button.isEnabled()
    assert window.ask_button.isEnabled() and window.record_button.isEnabled()
    assert window.transcript.toPlainText() == 'previous Voice Note' and recent == []
    window.close()
    app.processEvents()


@pytest.mark.parametrize('outcome', ['empty', 'whisper_failure', 'rag_failure', 'insufficient'])
def test_empty_and_safe_failures_return_idle_without_voice_note_changes(monkeypatch, outcome):
    app, window, _, recent = ready_window(monkeypatch)
    window.transcript.setPlainText('previous Voice Note')
    begin(window, monkeypatch)
    window.voice_ask_button.click()
    if outcome == 'empty':
        window._on_voice_question_finished(DesktopVoiceQuestionResult(''))
        assert window.rag_status.text() == 'No speech detected'
    elif outcome == 'whisper_failure':
        window._on_operation_failed('voice_question', 'Question transcription failed.')
        assert window.rag_status.text() == 'Question transcription failed.'
    else:
        from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
        failed = outcome == 'rag_failure'
        rag = DesktopRagResult(DesktopRagStatus.FAILED if failed else DesktopRagStatus.INSUFFICIENT_CONTEXT,
            '' if failed else INSUFFICIENT_CONTEXT_ANSWER,
            error_message='An answer could not be generated.' if failed else None)
        window._on_voice_question_finished(DesktopVoiceQuestionResult(QUERY, rag))
        assert window.query_input.text() == QUERY
        assert window.rag_status.text() == ('An answer could not be generated.' if failed else 'Not enough information')
    assert window.sources_table.rowCount() == 0
    assert window.record_button.isEnabled() and window.query_input.isEnabled() and window.voice_ask_button.isEnabled()
    assert window.voice_ask_button.text() == 'ASK BY VOICE'
    assert window.transcript.toPlainText() == 'previous Voice Note' and recent == []
    window.close()
    app.processEvents()


@pytest.mark.parametrize('stage', ['capture', 'whisper', 'rag'])
def test_worker_close_is_safe_during_each_voice_question_stage(tmp_path, stage):
    app = QApplication.instance() or QApplication([])
    h = harness(tmp_path, texts=(QUERY,))
    reader_started, reader_release = threading.Event(), threading.Event()
    errors = []
    if stage == 'whisper':
        original = h.provider.transcribe
        def read(path):
            reader_started.set()
            assert reader_release.wait(5) and path.exists()
        async def transcribe(request):
            await asyncio.to_thread(read, request.audio.path)
            return await original(request)
        h.provider.transcribe = transcribe
    elif stage == 'rag':
        async def block():
            await asyncio.sleep(180)
        h.rag.before = block
    def factory(*, progress_callback):
        asyncio.get_event_loop().set_exception_handler(lambda loop, context: errors.append(context))
        c = h.controller()
        c._progress_callback = progress_callback
        return c
    worker = AsyncDesktopThread(controller_factory=factory)
    success, failed = QSignalSpy(worker.voice_question_finished), QSignalSpy(worker.operation_failed)
    window = MainWindow(worker=worker)
    try:
        window.show()
        wait_until(lambda: window.voice_ask_button.isEnabled())
        window.voice_ask_button.click()
        wait_until(lambda: window.rag_status.text() == 'Listening...')
        if stage != 'capture':
            window.voice_ask_button.click()
            if stage == 'whisper':
                wait_until(reader_started.is_set)
            else:
                wait_until(lambda: window.rag_status.text() == 'Generating answer...')
        window.close()
        if stage == 'whisper':
            QTest.qWait(30)
            assert worker.isRunning() and list(tmp_path.glob('*.wav'))
            reader_release.set()
        wait_until(lambda: not worker.isRunning())
        app.processEvents()
        assert not window.isVisible() and not window._question_timer.isActive()
        assert success.count() == failed.count() == 0 and not errors
        assert not list(tmp_path.glob('*.wav'))
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == h.recorder.shutdown_count == 1
        if stage in {'capture', 'whisper'}:
            assert h.rag.requests == []
    finally:
        reader_release.set()
        worker.request_shutdown()
        assert worker.wait(2000)
        window.close()
