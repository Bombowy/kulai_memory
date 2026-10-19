"""Short opt-in Qwen3.5/BGE test on synthetic owned facts; no downloads."""

import asyncio
import json
import os
from time import perf_counter
from urllib.request import urlopen
from uuid import uuid4

import pytest

from backend.tests.approved_facts_support import derived_fingerprint
from kulai_memory.application.approved_facts import ApprovedFactsService, FactRagError
from kulai_memory.fact_persistence.retrieval import OwnedApprovedFactsReader
from kulai_memory.fact_rag_demo import synthetic_session
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.llm_provider import create_llm_provider
from kulai_memory.settings import get_settings


class ObservedSyntheticRun:
    """Diagnostics only in this explicit synthetic model test; never app logs."""
    def __init__(self,provider):
        self.provider=provider
        self.provider_id=provider.provider_id
        self.capabilities=provider.capabilities
        self.last={}

    async def generate(self,request):
        response=await self.provider.generate(request)
        try: self.last=json.loads(response.text)
        except ValueError: self.last={}
        return response


def test_short_real_qwen_bge_approved_facts():
    if not all(os.environ.get(k)=='1' for k in ('KULAI_RUN_POSTGRES_INTEGRATION','KULAI_RUN_LLM_INTEGRATION','KULAI_RUN_OLLAMA_INTEGRATION')):
        pytest.skip('NOT RUN: explicitly opt in to local PostgreSQL/Qwen/BGE.')
    try:
        with urlopen('http://127.0.0.1:11434/api/tags',timeout=5) as r:
            installed={m['name'] for m in json.load(r)['models']}
    except Exception: pytest.skip('NOT RUN: local Ollama unavailable.')
    if not {'qwen3.5:9b','bge-m3:567m-fp16'}<=installed:
        pytest.skip('NOT RUN: existing required models missing; no download.')

    async def scenario():
        settings=get_settings()
        assert settings.kulai_llm_model=='qwen3.5:9b'
        assert settings.kulai_embedding_model=='bge-m3:567m-fp16'
        async with synthetic_session() as (_,local):
            # Explicit test decisions under the trusted synthetic session;
            # launching the actual CLI never performs these approvals.
            for c in await local.service.list_pending():
                await local.service.approve(c.id,principal=local.principal,request_id=uuid4(),expected_version=c.lock_version)
            relation=next(r for r in await local.service.list_relations() if r.relation=='possible_supersession')
            await local.service.approve_relation(relation.id,principal=local.principal,request_id=uuid4(),expected_version=relation.lock_version)
            before=await derived_fingerprint(local.storage)
            async with create_embedding_provider(settings=settings) as bge, create_llm_provider(settings=settings) as llm:
                observed=ObservedSyntheticRun(llm)
                rag=ApprovedFactsService(reader=OwnedApprovedFactsReader(local.storage),embedding_provider=bge,llm_provider=observed)
                ranking=await rag.retrieve('Czy MCP jest już zaimplementowane?',top_k=1)
                assert ranking.hits[0].fact.information_type=='plan'
                for query in ('Czy MCP jest już zaimplementowane?', 'Jakiej technologii używa backend projektu X?'):
                    started=perf_counter()
                    try:
                        result=await rag.ask(query,top_k=4)
                    except FactRagError as exc:
                        print('REAL_FACT_RAG_ERROR='+json.dumps(dict(query=query,code=exc.code,synthetic_draft=observed.last)),flush=True)
                        raise
                    print('REAL_FACT_RAG='+json.dumps(dict(query=query,elapsed_s=round(perf_counter()-started,3),
                        result=result.model_dump(mode='json')),ensure_ascii=True),flush=True)
                    assert result.sufficient_context and result.citations
                    if 'MCP' in query:
                        assert any(f.information_type=='plan' for f in result.citations)
                        assert any(word in result.answer.casefold() for word in ('plan','zamiar','potwierdz','brak'))
                    else:
                        assert len(result.citations)>=2
                        assert 'FastAPI' in result.answer and 'Flask' in result.answer
                        assert not any(str(year) in result.answer for year in (2024,2025,2026,2027))
            assert await derived_fingerprint(local.storage)==before
    asyncio.run(asyncio.wait_for(scenario(),480))
