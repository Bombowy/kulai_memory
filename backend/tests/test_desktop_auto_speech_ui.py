"""Real Qt/AsyncDesktopThread/controller lifecycle, controlled models and audio."""
from __future__ import annotations
import asyncio
import os
import threading
from time import monotonic
from uuid import UUID

import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from backend.tests.test_desktop_speech import setup
from backend.tests.voice_question_fakes import OwnedWavRecorder
from kulai_memory.application.rag import MemoryCitation, MemoryRagError
from kulai_memory.desktop.app import AsyncDesktopThread, MainWindow
from kulai_memory.desktop.models import DesktopSpeechProgress, DesktopSpeechProgressState


def wait_until(condition):
    # Real worker/file cleanup can be delayed while opt-in models load. This is
    # only the test deadline; ordering is proved by the explicit drain barrier.
    deadline = monotonic() + 10
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


def running_window(tmp_path, *, outcome='answered', available=True):
    app = QApplication.instance() or QApplication([])
    h, c, llm, provider, player, runtime, result = setup(tmp_path)
    h.recorder = c._recorder = OwnedWavRecorder(tmp_path)
    h.provider._texts = ('Synthetic question',)
    if outcome == 'answered':
        h.rag.result = h.rag.result.model_copy(update={
            'answer': result.answer, 'sufficient_context': True,
            'citations': (MemoryCitation(memory_id=UUID(int=91), rank=2, score=.87),),
            'retrieval': h.rag.result.retrieval.model_copy(update={'context_memory_count': 1}),
        })
    elif outcome == 'failed':
        h.rag.error = MemoryRagError('rag.generation_failed')
    if not available:
        c._speech_runtime_factory = lambda **kwargs: None
    calls, rendered, threads, errors = [], [], [], []
    original_speak = c.speak_answer
    async def speak(*, result):
        calls.append(result)
        threads.append(threading.get_ident())
        return await original_speak(result=result)
    c.speak_answer = speak
    def factory(*, progress_callback):
        asyncio.get_event_loop().set_exception_handler(lambda loop, context: errors.append(context))
        c._progress_callback = progress_callback
        return c
    worker = AsyncDesktopThread(controller_factory=factory)
    class ObservedWindow(MainWindow):
        def _speak_answer(self):
            rendered.append((self.answer.toPlainText(), self.sources_table.rowCount()))
            super()._speak_answer()
    window = ObservedWindow(worker=worker)
    window.show()
    wait_until(lambda: window._rag_ready and not window._busy)
    return app, window, worker, h, c, provider, player, runtime, calls, rendered, threads, errors


def submit_question(window, origin):
    if origin == 'text':
        window.query_input.setText('Synthetic question')
        assert window.ask_button.isEnabled(), (window._busy, window._speech_active, window._speech_preparing, window._speech_stopping)
        window.ask_button.click()
    else:
        assert window.voice_ask_button.isEnabled(), (window._busy, window._speech_active, window._speech_preparing, window._speech_stopping)
        window.voice_ask_button.click()
        wait_until(lambda: window._question_recording)
        window.voice_ask_button.click()


@pytest.mark.parametrize('origin', ['text', 'voice'])
@pytest.mark.parametrize('outcome,enabled,available,expected', [
    ('answered', True, True, 1), ('answered', False, True, 0),
    ('failed', True, True, 0), ('insufficient', True, True, 1),
    ('answered', True, False, 0),
])
def test_full_request_auto_speech_once_after_render(tmp_path, origin, outcome, enabled, available, expected):
    app, w, worker, h, c, provider, player, runtime, calls, rendered, threads, errors = running_window(
        tmp_path, outcome=outcome, available=available)
    failed = QSignalSpy(worker.operation_failed)
    finished = QSignalSpy(worker.rag_finished if origin == 'text' else worker.voice_question_finished)
    try:
        w.auto_speak_checkbox.setChecked(enabled)
        submit_question(w, origin)
        wait_until(lambda: finished.count() == 1 and not w._rag_active and not w._speech_active)
        app.processEvents()
        assert len(calls) == expected and len(rendered) == expected
        assert not failed.count() and not errors
        assert len(h.rag.requests) == 1
        assert not h.repository.create_or_get_calls and not h.indexer.calls and not h.embedding.requests
        assert len(h.provider.requests) == int(origin == 'voice')
        assert h.rag_factory_calls == h.embedding_factory_calls == h.provider_factory_calls == 1
        assert not h.recorder.owned and not runtime.artifacts
        if expected:
            assert rendered == [(calls[0].answer, len(calls[0].citations))]
            assert w.answer.toPlainText() == calls[0].answer
            assert w.sources_table.rowCount() == len(calls[0].citations)
            assert all(thread != threading.get_ident() for thread in threads)
            assert w.speech_status.text() == 'Speech finished'
        else:
            assert not provider.calls
        # OFF disables automation, not the manual SPEAK action.
        if not enabled:
            w.speak_button.click()
            wait_until(lambda: len(calls) == 1 and not w._speech_active)
    finally:
        w.close()
        wait_until(lambda: not worker.isRunning())
        app.processEvents()
        assert h.embedding.closed == h.rag.closed == 1
        assert provider.closed == int(available)


@pytest.mark.parametrize('origin', ['text', 'voice'])
def test_new_request_during_auto_playback_drains_before_next_operation(tmp_path, origin):
    app, w, worker, h, c, provider, player, runtime, calls, _, _, errors = running_window(tmp_path)
    failed = QSignalSpy(worker.operation_failed)
    progress = QSignalSpy(worker.progress)
    drained, release = threading.Event(), threading.Event()
    timeline = []
    original_stop = player.stop
    async def stop():
        if len(calls) == 1:
            timeline.append('drain_started')
            drained.set()
            # Controlled native cleanup must finish before the next request acquires the lock.
            assert await asyncio.to_thread(release.wait, 20)
            timeline.append('drain_finished')
        await original_stop()
    player.stop = stop
    player.release.clear()
    original_ask = h.rag.ask
    async def ask(**kwargs):
        timeline.append('ask')
        return await original_ask(**kwargs)
    h.rag.ask = ask
    try:
        submit_question(w, origin)
        wait_until(lambda: player.entered.is_set() and not w._speech_preparing)
        paths = runtime.artifacts
        assert paths and len(calls) == 1
        submit_next = w.ask_button if origin == 'text' else w.voice_ask_button
        submit_next.click()
        wait_until(drained.is_set)
        assert len(h.rag.requests) == 1 and all(path.exists() for path in paths)
        assert c._operation_lock.locked()
        worker._loop.call_soon_threadsafe(player.release.set)
        release.set()
        if origin == 'voice':
            wait_until(lambda: w._question_recording)
            w.voice_ask_button.click()
        wait_until(lambda: len(calls) == 2 and not w._rag_active and not w._speech_active)
        assert timeline.index('drain_finished') < len(timeline) - 1
        assert timeline.count('ask') == 2
        assert timeline.index('drain_finished') < timeline.index('ask', 1)
        assert not failed.count() and not errors
        assert len(h.rag.requests) == len(calls) == 2
        assert not runtime.artifacts and all(not path.exists() for path in paths)
        states = [progress.at(i)[0].state for i in range(progress.count())
                  if isinstance(progress.at(i)[0], DesktopSpeechProgress)]
        assert states.count(DesktopSpeechProgressState.FINISHED) == 1
        assert provider.closed == 0 and w.speech_status.text() == 'Speech finished'
    finally:
        release.set()
        if worker._loop is not None:
            worker._loop.call_soon_threadsafe(player.release.set)
        w.close()
        wait_until(lambda: not worker.isRunning())
        app.processEvents()
        assert provider.closed == h.rag.closed == h.embedding.closed == 1


def test_auto_speech_failure_preserves_rendered_answer_and_citations(tmp_path):
    app, w, worker, h, c, provider, _, runtime, calls, _, _, errors = running_window(tmp_path)
    failed = QSignalSpy(worker.operation_failed)
    provider.error_at = 1
    try:
        submit_question(w, 'text')
        wait_until(lambda: failed.count() == 1 and not w._speech_active)
        assert len(calls) == 1 and w.answer.toPlainText() == h.rag.result.answer
        assert w.rag_status.text() == 'Answered' and w.sources_table.rowCount() == 1
        assert w.sources_table.item(0, 1).text() == str(UUID(int=91))
        assert w.speech_status.text() == 'Could not speak answer.' and w.speak_button.isEnabled()
        assert not runtime.artifacts and not errors
    finally:
        w.close()
        wait_until(lambda: not worker.isRunning())
        app.processEvents()
        assert provider.closed == h.rag.closed == h.embedding.closed == 1
