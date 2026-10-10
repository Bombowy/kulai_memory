"""One real local Qwen, synthetic answers only; no retrieval/database input."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from kulai_provider_ollama import provider as ollama_module

from backend.tests.polish_english_fakes import PL_EN_CASES, assert_language_coverage
from kulai_memory.application.speech import SPEECH_PLANNER_SYSTEM_PROMPT
from kulai_memory.rag_runtime import MemoryRagRuntime
from kulai_memory.settings import Settings

def test_real_qwen_exact_pl_en_mixed_same_runtime_client_no_context(monkeypatch):
    if os.environ.get('KULAI_RUN_LLM_INTEGRATION') != '1':
        pytest.skip('Set KULAI_RUN_LLM_INTEGRATION=1 for synthetic local Qwen speech plans.')
    counts = dict(clients=0, requests=0, closed=0)
    class Client(ollama_module.AsyncClient):
        def __init__(self, *args, **kwargs):
            counts['clients'] += 1
            super().__init__(*args, **kwargs)
        async def chat(self, *args, **kwargs):
            messages = kwargs['messages']
            assert len(messages) == 2 and messages[0]['content'] == SPEECH_PLANNER_SYSTEM_PROMPT
            data = json.loads(messages[1]['content'].split('\n', 1)[1])
            assert set(data) == {'answer'} and data['answer'] in tuple(case[0] for case in PL_EN_CASES)
            assert kwargs['model'] == 'qwen3.5:9b'
            counts['requests'] += 1
            return await super().chat(*args, **kwargs)
        async def close(self):
            counts['closed'] += 1
            await super().close()
    monkeypatch.setattr(ollama_module, 'AsyncClient', Client)
    def forbidden_database():
        pytest.fail('Speech planning must not create a database session.')
    async def scenario():
        runtime = MemoryRagRuntime(settings=Settings(_env_file=None, kulai_llm_model='qwen3.5:9b'),
                                  session_factory=forbidden_database, embedding_provider=object())
        async with runtime:
            for number, (answer, languages, phrases) in enumerate(PL_EN_CASES, 1):
                plan = await runtime.plan_speech(answer=answer)
                assert_language_coverage(plan, answer, languages, phrases)
                print('speech.plan.real.case=' + str(number) + ';' + json.dumps({
                    'segment_count': len(plan.segments), 'languages': languages,
                    'char_counts': [len(s.text) for s in plan.segments]}))
        await runtime.aclose()
        assert counts == dict(clients=1, requests=len(PL_EN_CASES), closed=1)
        print('speech.plan.real.qwen=' + json.dumps(counts, sort_keys=True) + ';db_sessions=0;memory_context=false')
    asyncio.run(asyncio.wait_for(scenario(), 600))
