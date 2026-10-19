"""Frozen 12-question evaluation through the real Desktop worker and providers."""

import hashlib
import json
import os
import re
from pathlib import Path
from time import perf_counter
from urllib.request import urlopen

import pytest
from PySide6.QtCore import QTimer

from backend.tests.integration.test_fact_workspace_postgres import workspace,approve_type
from backend.tests.integration.test_fact_review_postgres import wait_for,loaded,filter_by,select

DATA=Path(__file__).parents[1]/'fixtures'/'fact_workspace_eval_v1.json'


def quality(case,packet):
    # Small semantic rubric only; report must additionally review language and
    # nuance. A rubric_match is not a claim of complete answer correctness.
    result=packet.answer
    text=result.answer.casefold()
    rubric=case['rubric']
    if not result.sufficient_context: return 'safe_refusal','insufficient_context'
    if len(result.citations)<case['min_citations']: return 'error','missing_required_sources'
    if re.search(r'\b(?:19|20)\d{2}\b',text): return 'error','invented_year'
    good={
        'current':'fastapi' in text,
        'previous':'flask' in text,
        'month':any(v in text for v in ('paździer','october')),
        'year_unknown':any(v in text for v in ('nie','not','no ','brak','unspecified','unknown')),
        'removal_unknown':any(v in text for v in ('nie','not','no ','brak','cannot','can’t',"can't")),
        'plan':any(f.information_type=='plan' for f in result.citations) and not text.startswith(('tak,','yes,')),
        'preference':'python' in text and any(f.information_type=='preference' for f in result.citations),
        'insufficient':False,
    }[rubric]
    return ('correct','rubric_match') if good else ('error','content_requires_review')


def test_frozen_twelve_questions_real_desktop_qwen_bge():
    if not all(os.environ.get(k)=='1' for k in ('KULAI_RUN_POSTGRES_INTEGRATION','KULAI_RUN_LLM_INTEGRATION','KULAI_RUN_OLLAMA_INTEGRATION')):
        pytest.skip('NOT RUN: explicitly enable owned PG and real local models.')
    try:
        with urlopen('http://127.0.0.1:11434/api/tags',timeout=5) as response:
            models={m['name'] for m in json.load(response)['models']}
    except Exception: pytest.skip('NOT RUN: Ollama unavailable, no downloads.')
    if not {'qwen3.5:9b','bge-m3:567m-fp16'}<=models: pytest.skip('NOT RUN: required installed models missing.')
    dataset=json.loads(DATA.read_text(encoding='utf-8'))
    print('WORKSPACE_EVAL_DATASET_SHA256='+hashlib.sha256(DATA.read_bytes()).hexdigest(),flush=True)
    rows=[]; ticks=[]
    with workspace(real=True) as (w,boxes):
        approve_type(w,'fact'); approve_type(w,'plan'); approve_type(w,'preference')
        filter_by(w,'mode','relations'); filter_by(w,'relation_filter','possible_supersession')
        select(w); w.approve.click(); loaded(w)
        heartbeat=QTimer(w); heartbeat.setInterval(25); heartbeat.timeout.connect(lambda:ticks.append(True)); heartbeat.start()
        for case in dataset['questions']:
            w.tabs.setCurrentIndex(1); wait_for(lambda:not w._busy)
            w.question.setText(case['query'])
            started=perf_counter(); w.ask_button.click()
            wait_for(lambda:not w._busy,seconds=400)
            elapsed=round(perf_counter()-started,3)
            packet=w._packet
            if packet is None:
                code=w._last_error or 'operation_failed'
                category='safe_refusal' if code in {'fact_rag.invalid_citations','fact_rag.ungrounded_answer'} else 'error'
                row=dict(id=case['id'],query=case['query'],category=category,code=code,answer=None,seconds=elapsed,citations=[])
                assert not w.answer.toPlainText() and not w.citation_panel.source.toPlainText()
            else:
                category,reason=quality(case,packet)
                row=dict(id=case['id'],query=case['query'],category=category,code=reason,answer=packet.answer.answer,
                    seconds=elapsed,citations=[dict(candidate_id=str(f.reference.candidate_id),memory_id=str(f.reference.memory_id),
                        type=f.information_type,generation=f.reference.generation) for f in packet.facts])
                if packet.facts:
                    w.citations.setCurrentRow(0)
                    wait_for(lambda:not w._busy and bool(w.citation_panel.source.toPlainText()))
                    assert w.citation_panel.source.extraSelections()
            rows.append(row)
            print('WORKSPACE_EVAL='+json.dumps(row,ensure_ascii=True),flush=True)
        heartbeat.stop()
        assert ticks and len(rows)==12
        summary={key:sum(r['category']==key for r in rows) for key in ('correct','safe_refusal','error')}
        summary.update(questions=12,heartbeat_ticks=len(ticks),total_ask_s=round(sum(r['seconds'] for r in rows),3))
        print('WORKSPACE_EVAL_SUMMARY='+json.dumps(summary),flush=True)
        # This asserts execution/isolation, not 100% model quality or language
        # compliance. Every answer remains available for review; no retries.
