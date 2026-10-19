"""One real marker-owned DB behind both tabs; genuine decisions and lifecycle."""

import asyncio
import os
from contextlib import contextmanager,asynccontextmanager

import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from backend.tests.integration.test_memory_postgres import _require_opt_in
from backend.tests.integration.test_fact_review_postgres import wait_for,on_worker,loaded,select,filter_by
from backend.tests.approved_facts_support import AnswerLLM
from kulai_memory.database_safety import database_exists,database_snapshot_url,database_config
from kulai_memory.desktop.fact_workspace.backend import workspace_backend
from kulai_memory.desktop.fact_workspace.window import FactWorkspaceWindow
from kulai_memory.application.approved_facts.service import POLICY
from kulai_memory.deletion_persistence import delete_memory


@pytest.fixture(autouse=True)
def opt_in(): _require_opt_in()


@contextmanager
def workspace(*,factory=None,real=False):
    app=QApplication.instance() or QApplication([])
    boxes=[]
    @asynccontextmanager
    async def create():
        async with workspace_backend(real_models=real) as backend:
            boxes.append(backend)
            if factory: factory(backend)
            yield backend
    window=FactWorkspaceWindow(backend_factory=create,real_models=real,confirm=lambda _:True)
    window.show()
    try:
        wait_for(lambda: window._handles_loaded and window._view is not None and not window._busy or window._finished)
        assert not window._finished, window.message.text()
        yield window,boxes
    finally:
        name=boxes[0].database_name if boxes else None
        window.close(); wait_for(lambda:window._finished,seconds=25)
        assert window.worker.clean_shutdown
        if name: assert not asyncio.run(database_exists(name,config=database_config()))


def approve_type(w,type):
    w.tabs.setCurrentIndex(0)
    loaded(w)
    filter_by(w,'status_filter','PENDING'); filter_by(w,'type_filter',type)
    count=w._view.total_count
    for _ in range(count):
        select(w); w.approve.click(); loaded(w)
    return count


def ask(w,query):
    w.tabs.setCurrentIndex(1)
    wait_for(lambda:not w._busy)
    w.question.setText(query); w.ask_button.click()
    wait_for(lambda:not w._busy,seconds=60)
    return w._packet


def test_acceptance_one_database_approve_relation_ask_and_open_both_sources():
    with workspace() as (w,boxes):
        assert w._view.pending_count==4
        assert not ask(w,'backend').answer.sufficient_context
        assert approve_type(w,'plan')==1
        p=ask(w,'Czy MCP jest już zaimplementowane?')
        assert p.answer.sufficient_context and p.facts[0].information_type=='plan'
        w.citations.setCurrentRow(0)
        wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
        assert 'generation: 1' in w.generation.text()
        assert approve_type(w,'fact')==2
        w.tabs.setCurrentIndex(0); loaded(w)
        filter_by(w,'mode','relations'); filter_by(w,'relation_filter','possible_supersession')
        select(w); w.approve.click(); loaded(w)
        p=ask(w,'Jakiej technologii używa backend?')
        assert p.answer.sufficient_context and len(p.facts)==2
        for row in range(2):
            w.citations.setCurrentRow(row)
            wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
            assert w.citation_panel.source.extraSelections()
            assert '2026' not in w.answer.toPlainText()
        assert boxes[0]._local.storage._owned.name in w.database.text()
        assert len(boxes)==1


@pytest.mark.parametrize('action',['edit','archive','restore','delete'])
def test_explicit_test_lifecycle_clears_ui_and_never_reactivates(action):
    with workspace() as (w,boxes):
        approve_type(w,'fact')
        assert ask(w,'backend').answer.sufficient_context
        w.citations.setCurrentRow(0)
        wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
        w.tabs.setCurrentIndex(0); loaded(w)
        w.source_selector.setCurrentIndex(0)
        if action=='restore':
            w.mutations['archive'].click(); loaded(w)
        w.mutations[action].click(); loaded(w)
        assert not w.answer.toPlainText() and not w.citation_panel.source.toPlainText()
        packet=ask(w,'backend')
        a=boxes[0].source_ids[0]
        assert all(f.reference.memory_id!=a for f in packet.facts)
        if action=='delete':
            async def check():
                async with boxes[0]._local.storage._tx(read_only=True) as repo:
                    return await repo.one('SELECT id FROM memories WHERE id=:id',{'id':a})
            assert on_worker(w,check) is None


def test_reject_revoke_and_click_navigation_without_uuid_copy():
    with workspace() as (w,boxes):
        filter_by(w,'type_filter','preference'); select(w); w.reject.click(); loaded(w)
        assert approve_type(w,'plan')==1
        ask(w,'Czy MCP jest już zaimplementowane?')
        w.citations.setCurrentRow(0)
        wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
        w.show_review.click(); wait_for(lambda:not w._busy and w._detail is not None)
        assert w._detail.status=='APPROVED'
        w.revoke.click(); loaded(w)
        assert not ask(w,'Czy MCP jest już zaimplementowane?').answer.sufficient_context


def test_external_delete_erases_open_answer_source_and_cannot_reappear(caplog):
    with workspace() as (w,boxes):
        approve_type(w,'plan'); packet=ask(w,'MCP')
        w.citations.setCurrentRow(0)
        wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
        id=packet.facts[0].reference.memory_id
        async def remove():
            backend=boxes[0]
            from sqlalchemy.ext.asyncio import async_sessionmaker
            await delete_memory(memory_id=id,session_factory=async_sessionmaker(backend._local.storage._engine,expire_on_commit=False))
        on_worker(w,remove)
        wait_for(lambda:w._packet is None and not w.answer.toPlainText(),seconds=5)
        assert not w.citation_panel.source.toPlainText() and not w.citation_panel.source.extraSelections()
        w.sources_button.click(); wait_for(lambda:not w._busy and w._packet is not None)
        assert not w._packet.facts
        assert 'MCP' not in caplog.text


@pytest.mark.parametrize('denial',['citations','grounding'])
def test_refused_answer_never_shows_draft_sources_can_still_be_read(denial):
    def configure(backend):
        llm=AnswerLLM()
        llm.transform=lambda prompt,value: ({**value,'citations':['UNKNOWN']} if denial=='citations' and prompt==POLICY
            else {'supported':False} if denial=='grounding' and prompt!=POLICY else value)
        backend.rag.llm=llm
    with workspace(factory=configure) as (w,boxes):
        approve_type(w,'plan')
        assert ask(w,'MCP') is None
        assert not w.answer.toPlainText() and 'zablokow' in w.ask_status.text().casefold()
        w.sources_button.click(); wait_for(lambda:not w._busy and w._packet is not None)
        assert len(w._packet.facts)==1
        w.citations.setCurrentRow(0)
        wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))


def test_close_during_qwen_cancels_operation_then_cleans_database():
    entered=[]
    def configure(backend):
        llm=AnswerLLM()
        async def slow(request):
            entered.append(True)
            await asyncio.Event().wait()
        llm.hook=slow; backend.rag.llm=llm
    with workspace(factory=configure) as (w,boxes):
        approve_type(w,'plan')
        w.tabs.setCurrentIndex(1); w.question.setText('MCP'); w.ask_button.click()
        wait_for(lambda:bool(entered))
        assert not w.ask_button.isEnabled()
        w.close(); wait_for(lambda:w._finished)
        assert not w.answer.toPlainText() and not w.citation_panel.source.toPlainText()


def test_delete_during_qwen_and_explicit_cancel_are_safe():
    state={}
    def configure(backend):
        llm=AnswerLLM()
        async def pause(request):
            if request.messages[0].content==POLICY:
                state['release']=asyncio.Event()
                await state['release'].wait()
        llm.hook=pause; backend.rag.llm=llm; state['llm']=llm
    with workspace(factory=configure) as (w,boxes):
        approve_type(w,'plan')
        w.tabs.setCurrentIndex(1); w.question.setText('MCP'); w.ask_button.click(); w.ask_button.click()
        wait_for(lambda:'release' in state)
        assert len(state['llm'].calls)==1
        w.cancel_button.click(); wait_for(lambda:not w._busy)
        assert w._packet is None
        state.clear()
        # A NEW user operation; no automatic retry of the cancelled question.
        w.ask_button.click(); wait_for(lambda:'release' in state)
        async def remove():
            backend=boxes[0]
            from kulai_memory.desktop.fact_workspace.backend import Mutation
            source=(await backend.handles(None))[2]  # C, synthetic MCP plan.
            await backend.mutate(Mutation(source,'delete'))
            state['release'].set()
        on_worker(w,remove)
        wait_for(lambda:not w._busy)
        assert w._packet is None and not w.answer.toPlainText()
        assert w._last_error=='fact_rag.source_invalid'


def test_unicode_repeated_evidence_in_clicked_citation():
    with workspace() as (w,boxes):
        async def seed_unicode():
            from backend.tests.test_memory_intelligence import GoldenLLM,ConstantBGE,CASES
            from kulai_memory.application import Memory
            from kulai_memory.application.intelligence import MemoryIntelligenceService
            from kulai_memory.application.fact_memory import StageBatch,StageSource
            from kulai_memory.persistence import PostgresMemoryRepository
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from copy import deepcopy
            from uuid import uuid4
            case=deepcopy(CASES['pl_flask'])
            case['text']='🙂 𐐀 Zażółć\r\n'+case['text']+'\r\n'+case['text']
            backend=boxes[0]; local=backend._local
            intelligence=MemoryIntelligenceService(llm_provider=GoldenLLM(cases=[case]),embedding_provider=ConstantBGE())
            factory=async_sessionmaker(local.storage._engine,expire_on_commit=False)
            async with factory() as session:
                async with session.begin(): memory=await PostgresMemoryRepository(db=session).create(Memory(content=case['text']))
            source=await local.storage.memory_document(memory.id)
            result=await intelligence.extract(source)
            staged=await local.service.stage(StageBatch(sources=(StageSource(source=source,extraction=result),)))
            local.service._intelligence=intelligence
            await local.service.approve(staged.candidate_ids[0],principal=local.principal,request_id=uuid4(),expected_version=1)
            return case['text']
        original=on_worker(w,seed_unicode)
        packet=ask(w,'backend')
        assert len(packet.facts)==1
        w.citations.setCurrentRow(0)
        wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
        editor=w.citation_panel.source
        from kulai_memory.desktop.fact_review.evidence import qt_text_and_positions
        normalized,positions=qt_text_and_positions(original)
        assert editor.toPlainText()==normalized
        selections=editor.extraSelections()
        assert len(selections)==2
        assert {s.cursor.selectionStart() for s in selections}=={positions[e.start] for e in packet.facts[0].evidence}


def test_delete_archived_source_and_refuse_foreign_mutation_target():
    with workspace() as (w,boxes):
        from dataclasses import replace
        from uuid import uuid4
        from kulai_memory.desktop.fact_workspace.backend import Mutation
        from kulai_memory.application.fact_memory import FactMemoryError
        async def forbidden():
            source=(await boxes[0].handles(None))[0]
            with pytest.raises(FactMemoryError,match='forbidden'):
                await boxes[0].mutate(Mutation(replace(source,memory_id=uuid4()),'delete'))
        on_worker(w,forbidden)
        w.source_selector.setCurrentIndex(0)
        w.mutations['archive'].click(); loaded(w)
        assert w.mutations['delete'].isEnabled()
        w.mutations['delete'].click(); loaded(w)
        assert w.source_selector.currentData().deleted
