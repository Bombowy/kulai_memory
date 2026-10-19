"""Qt-only privacy, cancellation and refusal presentation."""

import os
from time import monotonic
from uuid import uuid4

import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication

from backend.tests.test_fact_review_ui import QuietWorker
from backend.tests.test_approved_facts import fact
from kulai_memory.application.approved_facts.models import FactAnswer
from kulai_memory.desktop.fact_workspace.backend import Display
from kulai_memory.desktop.fact_workspace.window import FactWorkspaceWindow
from kulai_memory.desktop.fact_workspace.worker import WorkspaceThread


class FakeWorker(QuietWorker):
    progress=Signal(int,str)
    denied=Signal(int,str)
    def cancel_read(self,*args): self.cancelled=args


@pytest.fixture
def ui():
    app=QApplication.instance() or QApplication([])
    worker=FakeWorker()
    window=FactWorkspaceWindow(worker=worker)
    window._ready=True; window._handles_loaded=True; window.tabs.setCurrentIndex(1)
    window._busy=False; window._enable()
    try: yield window,worker
    finally: window.close(); app.processEvents()


def display():
    f=fact()
    answer=FactAnswer(answer='PRIVATE_QUOTE_SENTINEL',sufficient_context=True,citations=(f,),relations=(),
        partial_coverage=True,partial_reasons=('candidate_limit',),scanned_candidates=50,context_chars=1000)
    return Display(uuid4(),(f,),answer,True)


def send_answer(window,worker):
    window.question.setText('backend'); window.ask_button.click()
    packet=display()
    worker.completed.emit(window._token,'ask',packet)
    return packet


def test_one_admission_and_no_auto_retry_on_refusal(ui):
    w,worker=ui
    w.question.setText('backend'); w.ask_button.click(); w.ask_button.click()
    assert len(worker.calls)==1 and not w.ask_button.isEnabled()
    worker.denied.emit(w._token,'fact_rag.invalid_citations')
    worker.failed.emit(w._token,'ask',WorkspaceThread.error_message(type('Error',(),{'code':'fact_rag.invalid_citations'})()))
    assert 'cytowania' in w.ask_status.text() and not w.answer.toPlainText() and w.citations.count()==0
    assert len(worker.calls)==1 and w.sources_button.isEnabled()
    w.ask_button.click()
    assert len(worker.calls)==2 and worker.calls[0][0]!=worker.calls[1][0]


def test_poll_keeps_answer_then_lease_expiry_scrubs_and_rejects_late_result(ui):
    w,worker=ui
    packet=send_answer(w,worker)
    revision=w.answer.document().revision()
    w._poll_refresh()
    assert w.answer.toPlainText()=='PRIVATE_QUOTE_SENTINEL'
    worker.completed.emit(w._token,'check_display',(packet.id,monotonic()))
    assert w.answer.document().revision()==revision and 'NIEPEŁNE' in w.coverage.text()
    w._poll_refresh(); old=w._token; w._expire_answer()
    assert not w.answer.toPlainText() and w.citations.count()==0
    worker.completed.emit(old,'check_display',(packet.id,monotonic()))
    assert w._packet is None and not w.citation_panel.source.toPlainText()


def test_cancel_close_and_filter_navigation_erase_private_state(ui):
    w,worker=ui
    send_answer(w,worker)
    w.citation_panel.source.setPlainText('PRIVATE_SOURCE')
    w.tabs.setCurrentIndex(0)
    assert not w.answer.toPlainText() and not w.citation_panel.source.toPlainText()
    w._busy=False; w.tabs.setCurrentIndex(1); w._enable()
    w.question.setText('backend'); w.ask_button.click()
    token=w._token; w.cancel_button.click()
    assert worker.cancelled==(token,'ask')
    worker.completed.emit(token,'ask',display())
    assert w._packet is None and not w.answer.toPlainText()


def test_old_queued_packet_is_never_displayed(ui):
    w,worker=ui
    w.question.setText('backend'); w.ask_button.click()
    p=display()
    from dataclasses import replace
    worker.completed.emit(w._token,'ask',replace(p,checked_at=monotonic()-10))
    assert w._packet is None and not w.answer.toPlainText()


def test_refusal_messages_do_not_expose_exception_payload():
    class Error(Exception): code='fact_rag.ungrounded_answer'
    assert 'PRIVATE_SECRET' not in WorkspaceThread.error_message(Error('PRIVATE_SECRET'))
    assert 'zablokowany' in WorkspaceThread.error_message(Error())


def test_archived_test_source_can_be_restored_or_deleted(ui):
    from kulai_memory.desktop.fact_workspace.backend import SourceHandle
    w,_=ui
    w.source_selector.addItem('A',SourceHandle('A',uuid4(),1,'epoch:test:2',True,False))
    w._enable()
    assert w.mutations['restore'].isEnabled() and w.mutations['delete'].isEnabled()
    assert not w.mutations['edit'].isEnabled() and not w.mutations['archive'].isEnabled()
