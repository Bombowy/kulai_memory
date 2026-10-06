"""Prepare Memory vectors before entering a caller-owned write transaction."""

from __future__ import annotations

from kulai_embeddings import EmbeddingProvider, EmbeddingRequest, embed
from kulai_vector_store import (
    VectorRecord,
    VectorStore,
    VectorUpsertRequest,
    VectorUpsertResult,
    upsert,
)

from kulai_memory.embedding_validation import validate_embedding_dimension

from .memory import Memory

MEMORY_VECTOR_NAMESPACE = "kulai_memory.memories.v1"


class MemoryIndexingError(RuntimeError):
    """Public indexing failure without Memory content or vector values."""

    safe_message = "Memory indexing could not be completed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class MemoryIndexingService:
    """Use neutral providers and stores without owning their lifecycle or commit.

    Call prepare with a detached Memory and no open write transaction. Only
    then open a short store session, call upsert, and commit as the caller.
    A prepared request may be reused after rollback without re-embedding.
    """

    def __init__(
        self, *, provider: EmbeddingProvider, expected_dimension: int | None
    ) -> None:
        self._provider = provider
        self._expected_dimension = expected_dimension

    async def prepare(self, *, memory: Memory) -> VectorUpsertRequest:
        """Embed exactly the canonical content and build one stable identity."""

        try:
            response = await embed(
                provider=self._provider,
                request=EmbeddingRequest(inputs=(memory.content,)),
            )
            validate_embedding_dimension(
                response, expected_dimension=self._expected_dimension
            )
            if response.model_id is None:
                raise MemoryIndexingError()
            record_id = str(memory.id)
            record = VectorRecord(
                id=record_id,
                vector=response.embeddings[0],
                metadata={
                    "source_memory_id": record_id,
                    "embedding_provider_id": response.provider_id,
                    "embedding_model_tag": response.model_id,
                    "embedding_dimension": response.dimension,
                },
            )
            return VectorUpsertRequest(
                namespace=MEMORY_VECTOR_NAMESPACE, records=(record,)
            )
        except Exception:
            raise MemoryIndexingError() from None

    async def upsert(
        self, *, store: VectorStore, request: VectorUpsertRequest
    ) -> VectorUpsertResult:
        """Persist a prepared vector; success still requires caller commit."""

        try:
            if (
                request.namespace != MEMORY_VECTOR_NAMESPACE
                or len(request.records) != 1
                or request.dimension != self._expected_dimension
            ):
                raise MemoryIndexingError()
            return await upsert(store=store, request=request)
        except Exception:
            raise MemoryIndexingError() from None
