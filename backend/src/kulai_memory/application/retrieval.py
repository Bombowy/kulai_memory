"""Prepare query embeddings and resolve ordered vector matches to Memory."""

from __future__ import annotations

from uuid import UUID

from kulai_embeddings import EmbeddingProvider, EmbeddingRequest, embed
from kulai_vector_store import (
    VectorMetric, VectorSearchRequest, VectorStore, search as vector_search,
)
from pydantic import BaseModel, ConfigDict, Field

from kulai_memory.embedding_validation import validate_embedding_dimension

from .indexing import MEMORY_VECTOR_NAMESPACE, memory_vector_metadata_matches
from .memory import Memory
from .ports import MemoryRepository


_MESSAGES = {
    "retrieval.invalid_query": "Query must be a non-blank string of at most 10000 characters.",
    "retrieval.invalid_top_k": "Top-k must be an integer between 1 and 20.",
    "retrieval.invalid_request": "The retrieval request is invalid.",
    "retrieval.embedding_failed": "Query embedding could not be completed.",
    "retrieval.dimension_mismatch": "Query embedding dimension is incompatible.",
    "retrieval.embedding_incompatible": "Query embedding model or provider is incompatible.",
    "retrieval.incompatible_metadata": "A vector match has incompatible embedding metadata.",
    "retrieval.invalid_record_id": "A vector match has an invalid Memory identifier.",
    "retrieval.vector_orphan": "A vector match refers to a missing Memory.",
    "retrieval.memory_archived": "A vector match refers to an archived Memory.",
    "retrieval.unsupported_metric": "The retrieval metric is incompatible.",
    "retrieval.operation_failed": "Memory retrieval could not be completed.",
}


class MemoryRetrievalError(RuntimeError):
    """Known safe code/message, without infrastructure or user payloads."""

    def __init__(self, code: str = "retrieval.operation_failed") -> None:
        self.code = code if code in _MESSAGES else "retrieval.operation_failed"
        super().__init__(_MESSAGES[self.code])


class _ResultModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True,
    )


class MemoryRetrievalHit(_ResultModel):
    """Canonical content and higher-is-better score; never an embedding vector."""

    memory: Memory
    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)
    vector_record_id: str


class MemoryRetrievalResult(_ResultModel):
    metric: VectorMetric
    hits: tuple[MemoryRetrievalHit, ...]


class MemoryRetrievalService:
    """Two phases let the host embed before opening a read-only store session."""

    def __init__(
        self, *, provider: EmbeddingProvider, expected_dimension: int,
        expected_provider_id: str, expected_model_tag: str,
    ) -> None:
        self._provider = provider
        self._dimension = expected_dimension
        self._provider_id = expected_provider_id
        self._model_tag = expected_model_tag

    @staticmethod
    def validate_input(*, query: str, top_k: int = 5) -> None:
        if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
            raise MemoryRetrievalError("retrieval.invalid_query")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise MemoryRetrievalError("retrieval.invalid_top_k")

    async def prepare(self, *, query: str, top_k: int = 5) -> VectorSearchRequest:
        """Internal store request only; do not render/log its query vector."""

        self.validate_input(query=query, top_k=top_k)
        try:
            response = await embed(
                provider=self._provider, request=EmbeddingRequest(inputs=(query,)),
            )
        except Exception:
            raise MemoryRetrievalError("retrieval.embedding_failed") from None
        try:
            validate_embedding_dimension(response, expected_dimension=self._dimension)
        except Exception:
            raise MemoryRetrievalError("retrieval.dimension_mismatch") from None
        if response.provider_id != self._provider_id or response.model_id != self._model_tag:
            raise MemoryRetrievalError("retrieval.embedding_incompatible")
        if not any(value != 0.0 for value in response.embeddings[0].values):
            # Cosine distance is undefined for an all-zero query vector.
            raise MemoryRetrievalError("retrieval.embedding_failed")
        try:
            return VectorSearchRequest(
                vector=response.embeddings[0], top_k=top_k, namespace=MEMORY_VECTOR_NAMESPACE,
            )
        except Exception:
            raise MemoryRetrievalError("retrieval.invalid_request") from None

    async def search(
        self, *, store: VectorStore, repository: MemoryRepository,
        request: VectorSearchRequest,
    ) -> MemoryRetrievalResult:
        """Fail the whole retrieval on incompatible metadata or broken identity."""

        try:
            if (
                not isinstance(request, VectorSearchRequest)
                or request.namespace != MEMORY_VECTOR_NAMESPACE
                or request.vector.dimension != self._dimension
                or request.filters
                or not any(value != 0.0 for value in request.vector.values)
            ):
                raise MemoryRetrievalError("retrieval.invalid_request")
            self.validate_input(query="prepared query", top_k=request.top_k)
            result = await vector_search(store=store, request=request)
            if result.metric != VectorMetric.COSINE:
                raise MemoryRetrievalError("retrieval.unsupported_metric")

            identities: list[UUID] = []
            for match in result.matches:
                metadata = match.record.metadata
                try:
                    memory_id = UUID(match.record.id)
                    if str(memory_id) != match.record.id:
                        raise ValueError
                except ValueError:
                    raise MemoryRetrievalError("retrieval.invalid_record_id") from None
                if not memory_vector_metadata_matches(
                    metadata, memory_id=memory_id, provider_id=self._provider_id,
                    model_tag=self._model_tag, dimension=self._dimension,
                ):
                    raise MemoryRetrievalError("retrieval.incompatible_metadata")
                identities.append(memory_id)

            hits: list[MemoryRetrievalHit] = []
            for rank, (match, memory_id) in enumerate(zip(result.matches, identities), start=1):
                memory = await repository.get_by_id(memory_id)
                if memory is None:
                    raise MemoryRetrievalError("retrieval.vector_orphan")
                if not isinstance(memory, Memory) or memory.id != memory_id:
                    raise MemoryRetrievalError()
                if memory.archived_at is not None:
                    raise MemoryRetrievalError("retrieval.memory_archived")
                if not memory_vector_metadata_matches(
                    match.record.metadata, memory_id=memory_id, memory_revision=memory.revision,
                    provider_id=self._provider_id, model_tag=self._model_tag, dimension=self._dimension,
                ):
                    raise MemoryRetrievalError("retrieval.incompatible_metadata")
                hits.append(MemoryRetrievalHit(
                    memory=memory, rank=rank, score=match.score, vector_record_id=match.record.id,
                ))
            return MemoryRetrievalResult(metric=result.metric, hits=tuple(hits))
        except MemoryRetrievalError:
            raise
        except Exception:
            raise MemoryRetrievalError() from None
