"""Host text RAG lifecycle and short canonical retrieval transaction."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack

from kulai_embeddings import EmbeddingProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.rag import (
    MemoryContext, MemoryRagError, MemoryRagResult, answer_memory, build_memory_context,
)
from .application.retrieval import MemoryRetrievalService
from .application.speech import SpeechError, SpeechPlan, plan_speech
from .backfill import EMBEDDING_MODEL, VECTOR_DIMENSION
from .embedding_provider import create_embedding_provider
from .llm_provider import create_llm_provider
from .retrieval_persistence import retrieve_memories
from .settings import Settings


class MemoryRagRuntime:
    """One BGE and one Qwen client per scope, reusable across questions.

    The caller owns the engine. A supplied embedding provider is borrowed,
    never closed here. Otherwise this runtime owns its BGE as in the CLI.
    Qwen is always owned by this runtime. No Whisper is constructed.
    Context is returned only through the explicit local debugging method.
    """

    def __init__(
        self, *, settings: Settings, session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._settings = settings
        self._factory = session_factory
        self._embedding_provider = embedding_provider
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MemoryRagRuntime:
        if self._stack is not None:
            raise MemoryRagError()
        stack = AsyncExitStack()
        try:
            bge = self._embedding_provider
            if bge is None:
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
        await self.aclose()

    async def aclose(self) -> None:
        """Close owned providers once; never close the borrowed BGE."""

        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    async def ask(
        self, *, query: str, top_k: int = 5, on_generating: Callable[[], None] | None = None,
    ) -> MemoryRagResult:
        result, _ = await self.ask_with_context(query=query, top_k=top_k, on_generating=on_generating)
        return result

    async def plan_speech(self, *, answer: str) -> SpeechPlan:
        """Reuse the owned Qwen; planner receives only final answer, with no DB work."""
        if self._stack is None:
            raise SpeechError()
        return await plan_speech(answer=answer, provider=self._llm)

    async def ask_with_context(
        self, *, query: str, top_k: int = 5, on_generating: Callable[[], None] | None = None,
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
        if context.hits and on_generating is not None:
            on_generating()
        result = await answer_memory(
            query=query, retrieval=retrieval, llm_provider=self._llm,
        )
        return result, context
