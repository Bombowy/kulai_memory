"""Isolated desktop provider/runtime fixtures: no network or model loading."""

from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_vector_store import VectorMetric

from kulai_memory.application.rag import (
    INSUFFICIENT_CONTEXT_ANSWER, MemoryRagResult, MemoryRagRetrievalMetadata,
)


class FakeOwnedBGE:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities()

    def __init__(self):
        self.closed = 0
        self.requests = []

    async def embed(self, request):
        self.requests.append(request)
        return EmbeddingResponse(provider_id="ollama", model_id="bge-m3:567m-fp16", dimension=1024,
            embeddings=(EmbeddingVector(values=(1.0,) + (0.0,) * 1023),))

    async def aclose(self):
        self.closed += 1


class FakeDesktopRag:
    def __init__(self):
        self.requests = []
        self.entered = self.closed = 0
        self.provider = None
        self.error = None
        self.before = None
        self.result = MemoryRagResult(
            query="synthetic", answer=INSUFFICIENT_CONTEXT_ANSWER, sufficient_context=False, citations=(),
            retrieval=MemoryRagRetrievalMetadata(metric=VectorMetric.COSINE,
                retrieved_count=0, context_memory_count=0, context_chars=0, max_context_chars=12000),
        )

    async def __aenter__(self):
        self.entered += 1
        return self

    async def ask(self, *, query, top_k=5, on_generating=None):
        self.requests.append((query, top_k))
        if on_generating and (self.result.retrieval.context_memory_count or self.before or self.error):
            on_generating()
        if self.before:
            await self.before()
        if self.error:
            raise self.error
        return self.result

    async def aclose(self):
        self.closed += 1
