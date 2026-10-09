from __future__ import annotations

import asyncio
import traceback
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from kulai_embeddings import EmbeddingResponse, EmbeddingVector

from kulai_memory.application.indexing import (
    MemoryIndexingService, MemoryIndexingError, MemoryIndexingIncompatibleError,
    MemoryIndexingMissingError, MemoryIndexingState, EnsureMemoryIndexedResult,
)
from kulai_memory.application.memory import Memory
from kulai_memory import indexing_persistence as adapter
from kulai_memory.automatic_indexing import AutomaticMemoryIndexer
from kulai_memory.settings import Settings

MODEL = "bge-m3:567m-fp16"
PRIVATE = "PRIVATE_MEMORY_VECTOR_PASSWORD_SENTINEL"


def metadata(memory):
    return dict(source_memory_id=str(memory.id), embedding_provider_id="ollama",
                embedding_model_tag=MODEL, embedding_dimension=1024, revision=memory.revision)


class Provider:
    provider_id = "ollama"

    def __init__(self):
        from kulai_embeddings import EmbeddingCapabilities
        self.capabilities = EmbeddingCapabilities()
        self.requests = []
        self.closed = 0
        self.error = None
        self.before = lambda: None

    async def embed(self, request):
        self.before()
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return EmbeddingResponse(provider_id="ollama", model_id=MODEL, dimension=1024,
                                 embeddings=(EmbeddingVector(values=(1.0,) * 1024),))

    async def aclose(self):
        self.closed += 1


class Sessions:
    def __init__(self, memory):
        self.memory = memory
        self.active = 0
        self.writes = 0
        self.commits = 0
        self.commit_error = None

    @asynccontextmanager
    async def __call__(self):
        self.active += 1
        try:
            yield self
        finally:
            self.active -= 1

    async def execute(self, statement):
        pass

    async def scalar(self, statement):
        return self.memory

    async def rollback(self):
        pass

    @asynccontextmanager
    async def begin(self):
        yield
        if self.commit_error:
            raise self.commit_error
        self.commits += 1


@pytest.mark.parametrize("existing,raced,expected", [
    (False, False, MemoryIndexingState.INDEXED),
    (True, False, MemoryIndexingState.ALREADY_INDEXED),
    (False, True, MemoryIndexingState.ALREADY_INDEXED),
])
def test_ensure_checks_twice_and_never_embeds_with_session(monkeypatch, existing, raced, expected):
    async def scenario():
        memory = Memory(content=PRIVATE)
        sessions = Sessions(memory)
        provider = Provider()
        provider.before = lambda: check_no_session(sessions)
        checks = []
        async def read(db, memory_id):
            checks.append(memory_id)
            return metadata(memory) if existing or (raced and len(checks) == 2) else None
        async def lock(**kwargs):
            assert sessions.active == 1
        class Store:
            def __init__(self, **kwargs): pass
        service = MemoryIndexingService(provider=provider, expected_dimension=1024)
        async def write(**kwargs): sessions.writes += 1
        monkeypatch.setattr(adapter, "_vector_metadata", read)
        monkeypatch.setattr(adapter, "_lock_memory_for_indexing", lock)
        monkeypatch.setattr(adapter, "PgVectorStore", Store)
        monkeypatch.setattr(service, "upsert", write)
        result = await adapter.ensure_memory_indexed(memory=memory, service=service, session_factory=sessions)
        assert result.memory_id == memory.id and result.state is expected
        assert len(provider.requests) == (0 if existing else 1)
        assert sessions.writes == (1 if expected is MemoryIndexingState.INDEXED else 0)
        assert sessions.active == 0
        if provider.requests:
            request = provider.requests[0]
            assert request.inputs == (PRIVATE,) and request.model_hint is request.purpose is None
    asyncio.run(scenario())


def check_no_session(sessions):
    assert sessions.active == 0


@pytest.mark.parametrize("when", ["first", "second"])
@pytest.mark.parametrize("field", ["source_memory_id", "embedding_provider_id", "embedding_model_tag", "embedding_dimension", "revision"])
def test_incompatible_never_overwrites(monkeypatch, when, field):
    async def scenario():
        memory = Memory(content=PRIVATE)
        sessions = Sessions(memory)
        provider = Provider()
        calls = 0
        bad = metadata(memory)
        bad.pop(field)
        async def read(*args):
            nonlocal calls
            calls += 1
            return bad if when == "first" or calls == 2 else None
        async def lock(**kwargs): pass
        monkeypatch.setattr(adapter, "_vector_metadata", read)
        monkeypatch.setattr(adapter, "_lock_memory_for_indexing", lock)
        with pytest.raises(MemoryIndexingIncompatibleError) as caught:
            await adapter.ensure_memory_indexed(memory=memory, service=MemoryIndexingService(provider=provider, expected_dimension=1024), session_factory=sessions)
        assert PRIVATE not in str(caught.value)
        assert sessions.writes == 0 and sessions.memory == memory
        assert len(provider.requests) == (0 if when == "first" else 1)
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError(PRIVATE), asyncio.CancelledError()])
def test_embedding_error_preserves_memory_and_cancellation(monkeypatch, error):
    async def scenario():
        memory = Memory(content=PRIVATE)
        sessions = Sessions(memory)
        provider = Provider()
        provider.error = error
        async def read(*args): return None
        monkeypatch.setattr(adapter, "_vector_metadata", read)
        with pytest.raises(asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else MemoryIndexingError) as caught:
            await adapter.ensure_memory_indexed(memory=memory, service=MemoryIndexingService(provider=provider, expected_dimension=1024), session_factory=sessions)
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert sessions.writes == 0 and sessions.memory == memory and sessions.active == 0
    asyncio.run(scenario())


def test_indexer_bounded_repair_skips_incompatible_and_reuses_closes_provider(monkeypatch):
    async def scenario():
        memory = Memory(content=PRIVATE)
        provider = Provider()
        constructed = []
        def factory(**kwargs):
            constructed.append(kwargs)
            return provider
        indexer = AutomaticMemoryIndexer(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
                                        session_factory=None, provider_factory=factory)
        limits = []
        async def selection(limit):
            limits.append(limit)
            return ((memory.id,), 1, 1, 1) if len(limits) == 1 else ((), 2, 1, 0)
        async def read(memory_id): return memory
        async def ensure(memory): return EnsureMemoryIndexedResult(memory.id, MemoryIndexingState.INDEXED)
        monkeypatch.setattr(indexer, "_selection", selection)
        monkeypatch.setattr(indexer, "_read_memory", read)
        monkeypatch.setattr(indexer, "_ensure", ensure)
        report = await indexer.reconcile(limit=2)
        assert limits == [2, 1]
        assert report.indexed == report.incompatible_existing == 1 and report.degraded
        assert len(constructed) == 1
        await indexer.aclose()
        await indexer.aclose()
        assert provider.closed == 1
        with pytest.raises(MemoryIndexingError): await indexer.ensure(memory=memory)
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
def test_reconcile_rejects_unbounded_limit(limit):
    async def scenario():
        provider = Provider()
        indexer = AutomaticMemoryIndexer(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
                                        session_factory=None, provider_factory=lambda **kwargs: provider)
        try:
            with pytest.raises(MemoryIndexingError): await indexer.reconcile(limit=limit)
        finally:
            await indexer.aclose()
    asyncio.run(scenario())


def test_provider_close_finishes_before_cancellation_propagates():
    async def scenario():
        provider = Provider()
        entered, release = asyncio.Event(), asyncio.Event()
        original = provider.aclose
        async def close():
            entered.set()
            await release.wait()
            await original()
        provider.aclose = close
        indexer = AutomaticMemoryIndexer(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
                                        session_factory=None, provider_factory=lambda **kwargs: provider)
        operation = asyncio.create_task(indexer.aclose())
        await entered.wait()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await operation
        await indexer.aclose()
        assert provider.closed == 1
    asyncio.run(scenario())


def test_embedding_timeout_is_safe_and_leaves_memory(monkeypatch):
    async def scenario():
        memory = Memory(content=PRIVATE)
        sessions = Sessions(memory)
        provider = Provider()
        async def embed(request): await asyncio.Event().wait()
        async def read(*args): return None
        provider.embed = embed
        monkeypatch.setattr(adapter, "_vector_metadata", read)
        with pytest.raises(MemoryIndexingError):
            await adapter.ensure_memory_indexed(memory=memory,
                service=MemoryIndexingService(provider=provider, expected_dimension=1024),
                session_factory=sessions, embedding_timeout_seconds=0.01)
        assert sessions.memory == memory and sessions.writes == sessions.active == 0
    asyncio.run(scenario())
