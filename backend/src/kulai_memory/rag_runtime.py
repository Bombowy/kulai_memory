"""Host text RAG lifecycle and short canonical retrieval transaction."""

from __future__ import annotations

from contextlib import AsyncExitStack

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.rag import (
    MemoryContext, MemoryRagError, MemoryRagResult, answer_memory, build_memory_context,
)
from .application.retrieval import MemoryRetrievalService
from .backfill import EMBEDDING_MODEL, VECTOR_DIMENSION
from .embedding_provider import create_embedding_provider
from .llm_provider import create_llm_provider
from .retrieval_persistence import retrieve_memories
from .settings import Settings


class MemoryRagRuntime:
    """One BGE and one Qwen client per scope, reusable across questions.

    The caller owns the engine. No Whisper or voice runtime is constructed.
    Context is returned only through the explicit local debugging method.
    """

    def __init__(self, *, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._factory = session_factory
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MemoryRagRuntime:
        if self._stack is not None:
            raise MemoryRagError()
        stack = AsyncExitStack()
        try:
            bge = await stack.enter_async_context(create_embedding_provider(settings=self._settings))
            self._llm = await stack.enter_async_context(create_llm_provider(settings=self._settings))
            self._retrieval = MemoryRetrievalService(
                provider=bge, expected_dimension=VECTOR_DIMENSION,
                expected_provider_id="ollama", expected_model_tag=EMBEDDING_MODEL,
            )
        except BaseException as exc:
            await stack.aclose()
            if isinstance(exc, Exception):
                raise MemoryRagError("rag.invalid_configuration") from None
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *args) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    async def ask(self, *, query: str, top_k: int = 5) -> MemoryRagResult:
        result, _ = await self.ask_with_context(query=query, top_k=top_k)
        return result

    async def ask_with_context(
        self, *, query: str, top_k: int = 5,
    ) -> tuple[MemoryRagResult, MemoryContext]:
        MemoryRetrievalService.validate_input(query=query, top_k=top_k)
        if self._stack is None:
            raise MemoryRagError()
        try:
            retrieval = await retrieve_memories(
                query=query, top_k=top_k, service=self._retrieval, session_factory=self._factory,
            )
        except Exception:
            raise MemoryRagError("rag.retrieval_failed") from None
        # retrieve_memories has rolled back and closed its snapshot/session here.
        context = build_memory_context(retrieval)
        result = await answer_memory(
            query=query, retrieval=retrieval, llm_provider=self._llm,
        )
        return result, context
