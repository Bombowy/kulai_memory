"""Controlled provider hooks and technical fingerprints; synthetic data only."""

import hashlib
import json

from kulai_llm import LLMResponse

from backend.tests.test_memory_intelligence import ConstantBGE
from kulai_memory.application.approved_facts import ApprovedFactsService
from kulai_memory.fact_rag_demo import SyntheticAnswerLLM
from kulai_memory.fact_persistence.retrieval import OwnedApprovedFactsReader
from kulai_memory.fact_persistence.schema_v2 import TABLES


class AnswerLLM(SyntheticAnswerLLM):
    def __init__(self):
        self.calls = []
        self.hook = None
        self.transform = None

    async def generate(self, request):
        self.calls.append(request)
        if self.hook:
            await self.hook(request)
        response = await super().generate(request)
        value = json.loads(response.text)
        if self.transform:
            value = self.transform(request.messages[0].content, value)
        return LLMResponse(text=value if isinstance(value,str) else json.dumps(value), provider_id="ollama", model_id="qwen3.5:9b")


class HookBGE(ConstantBGE):
    def __init__(self):
        super().__init__()
        self.hook = None

    async def embed(self, request):
        if self.hook:
            await self.hook(request)
        return await super().embed(request)


def rag_for(local, *, bge=None, llm=None, **kwargs):
    return ApprovedFactsService(reader=OwnedApprovedFactsReader(local.storage),
        embedding_provider=bge or HookBGE(), llm_provider=llm or AnswerLLM(), **kwargs)


async def derived_fingerprint(storage):
    digest = hashlib.sha256()
    async with storage._tx(read_only=True) as repo:
        for table in TABLES:
            rows = await repo.rows(f"SELECT row_to_json(t)::text AS body FROM public.{table} t ORDER BY body")
            digest.update(json.dumps([table, [r['body'] for r in rows]], separators=(',',':')).encode())
    return digest.hexdigest()
