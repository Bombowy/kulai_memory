"""Opt-in local provider lifecycle and optional canonical read-only lookup."""

from __future__ import annotations

from contextlib import AsyncExitStack

from .application.intelligence import (
    IntelligenceError, MemoryIntelligenceService, PreviewResult, SourceDocument,
)
from .application.retrieval import MemoryRetrievalService
from .embedding_provider import create_embedding_provider
from .llm_provider import create_llm_provider
from .retrieval_persistence import retrieve_memories
from .settings import Settings


class MemoryIntelligenceRuntime:
    """Own one Qwen and one BGE client; borrow an optional session factory."""

    def __init__(self, *, settings: Settings, session_factory=None) -> None:
        self._settings = settings
        self._factory = session_factory
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self):
        if self._stack is not None:
            raise IntelligenceError()
        settings = self._settings
        if (
            settings.kulai_llm_model != "qwen3.5:9b"
            or settings.kulai_embedding_model != "bge-m3:567m-fp16"
            or settings.kulai_vector_dimension not in (None, 1024)
        ):
            raise IntelligenceError("invalid_configuration")
        stack = AsyncExitStack()
        try:
            bge = await stack.enter_async_context(create_embedding_provider(settings=settings))
            llm = await stack.enter_async_context(create_llm_provider(settings=settings))
            self._service = MemoryIntelligenceService(llm_provider=llm, embedding_provider=bge)
            self._retrieval = MemoryRetrievalService(
                provider=bge, expected_dimension=1024,
                expected_provider_id="ollama", expected_model_tag="bge-m3:567m-fp16",
            )
        except BaseException as exc:
            await stack.aclose()
            if isinstance(exc, Exception):
                raise IntelligenceError("invalid_configuration") from None
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    @property
    def intelligence(self) -> MemoryIntelligenceService:
        """Borrow the existing verifier while this runtime owns its providers."""
        if self._stack is None:
            raise IntelligenceError("invalid_configuration")
        return self._service

    async def preview(
        self, source: SourceDocument, references: tuple[SourceDocument, ...] = (), *,
        compare_main: bool = False, top_k: int = 5,
    ) -> PreviewResult:
        MemoryIntelligenceService.validate_top_k(top_k)
        if self._stack is None or (compare_main and self._factory is None):
            raise IntelligenceError("invalid_configuration")
        sources = (source, *references)
        if len({item.source_id for item in sources}) != len(sources):
            raise IntelligenceError("invalid_input")
        extractions = [await self._service.extract(item) for item in sources]
        if compare_main:
            memories = {}
            try:
                for candidate in extractions[0].candidates:
                    retrieval = await retrieve_memories(
                        query=candidate.fact.text, top_k=top_k, service=self._retrieval,
                        session_factory=self._factory,
                    )
                    for hit in retrieval.hits:
                        memories[hit.memory.id, hit.memory.revision] = hit.memory
            except Exception:
                raise IntelligenceError("retrieval_failed") from None
            # No session or read snapshot survives into extraction/generation.
            for memory in memories.values():
                try:
                    document = SourceDocument(
                        source_id=f"memory-{memory.id}-r{memory.revision}", text=memory.content,
                        memory_id=memory.id, revision=memory.revision, created_at=memory.created_at,
                        speaker_id=source.speaker_id,
                    )
                except Exception:
                    raise IntelligenceError("invalid_input") from None
                extractions.append(await self._service.extract(document))
        return await self._service.compare(
            tuple(extractions), top_k=top_k,
            comparison_scope="local_and_indexed_main" if compare_main else "local_sources",
        )
