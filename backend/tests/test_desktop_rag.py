"""Desktop questions reuse the canonical RAG; no network or model in unit tests."""
from __future__ import annotations

import asyncio
from dataclasses import fields
from uuid import UUID

import pytest

from backend.tests.test_desktop_controller import Harness
from backend.tests.test_memory_rag import FakeLLM, evidence, PRIVATE
from kulai_memory import rag_runtime
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER, MemoryRagError, answer_memory
from kulai_memory.automatic_indexing import AutomaticMemoryIndexer
from kulai_memory.desktop.models import (
    DesktopRagConfigurationError, DesktopRagInputError, DesktopRagProgressState,
    DesktopRagResult, DesktopRagStatus, DesktopStateError,
)
from kulai_memory.settings import Settings


def test_answer_and_citations_preserved_without_voice_save_or_recent_refresh(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        h.rag.result = await answer_memory(query='Where?', retrieval=evidence('Venus', 'Soup'),
                                          llm_provider=FakeLLM(ids=(str(UUID(int=2)), str(UUID(int=1)))))
        c = h.controller()
        await c.startup()
        sessions = len(h.sessions.sessions)
        for _ in range(2):
            result = await c.ask_memory(query=' exact question ', top_k=3)
            assert result.status is DesktopRagStatus.ANSWERED and result.answer == 'Wenus'
            assert [(x.memory_id, x.rank, x.score) for x in result.citations] == [
                (UUID(int=2), 2, 0.5), (UUID(int=1), 1, 1.0)]
        assert h.rag.requests == [(' exact question ', 3)] * 2
        assert h.rag.entered == h.rag_factory_calls == h.embedding_factory_calls == h.provider_factory_calls == 1
        assert h.rag.provider is h.embedding
        assert [p.state for p in h.progress] == [DesktopRagProgressState.RETRIEVING,
                                               DesktopRagProgressState.GENERATING] * 2
        assert len(h.sessions.sessions) == sessions  # no recent refresh / ingestion session
        assert h.repository.create_or_get_calls == h.provider.requests == h.indexer.calls == []
        assert h.recorder.started_devices == [] and not c.has_pending_save
        await c.shutdown()
        await c.shutdown()
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


def test_insufficient_context_uses_canonical_answer_and_zero_citations(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        c = h.controller()
        await c.startup()
        result = await c.ask_memory(query='Unsupported question')
        assert result.status is DesktopRagStatus.INSUFFICIENT_CONTEXT
        assert result.answer == INSUFFICIENT_CONTEXT_ANSWER and result.citations == ()
        assert {f.name for f in fields(DesktopRagResult)} == {'status', 'answer', 'citations', 'error_message'}
        await c.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('query,top_k', [('', 5), (' \t', 5), ('x' * 10001, 5),
                                       ('q', 0), ('q', 21), ('q', True), ('q', 1.5)])
def test_invalid_input_rejected_before_startup_provider_or_db(tmp_path, query, top_k):
    async def scenario():
        h = Harness(tmp_path)
        with pytest.raises(DesktopRagInputError) as caught:
            await h.controller().ask_memory(query=query, top_k=top_k)
        assert str(caught.value) == DesktopRagInputError.safe_message
        assert h.rag.requests == h.sessions.sessions == h.progress == []
        assert h.embedding_factory_calls == h.provider_factory_calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('error,message', [
    (MemoryRagError('rag.retrieval_failed'), 'Memory search could not be completed.'),
    (MemoryRagError('rag.invalid_configuration'), 'Memory assistant configuration is invalid.'),
    (MemoryRagError(), 'An answer could not be generated.'),
    (RuntimeError(PRIVATE), 'An answer could not be generated.'),
])
def test_failures_are_safe_results_without_private_query_context_or_stale_answer(tmp_path, error, message):
    async def scenario():
        h = Harness(tmp_path)
        h.rag.error = error
        c = h.controller()
        await c.startup()
        result = await c.ask_memory(query=PRIVATE)
        assert result.status is DesktopRagStatus.FAILED
        assert result.error_message == message and result.answer == '' and result.citations == ()
        assert PRIVATE not in repr(result)
        h.rag.error = None
        assert (await c.ask_memory(query='next')).status is DesktopRagStatus.INSUFFICIENT_CONTEXT
        await c.shutdown()
    asyncio.run(scenario())


def test_shutdown_cancels_generation_without_waiting_for_timeout_or_stale_success(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def block():
            started.set()
            try:
                await asyncio.sleep(180)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        h.rag.before = block
        c = h.controller()
        await c.startup()
        task = asyncio.create_task(c.ask_memory(query='question'))
        await started.wait()
        for operation in (c.start_recording(device_id=3), c.ask_memory(query='second'), c.retry_save()):
            with pytest.raises(DesktopStateError):
                await operation
        await asyncio.wait_for(c.shutdown(), timeout=1)
        assert cancelled.is_set() and task.cancelled()
        assert h.embedding.closed == h.rag.closed == h.indexer.closed == h.engine.dispose_count == 1
        assert h.repository.create_or_get_calls == h.provider.requests == h.indexer.calls == []
        assert c.provider is None and c._rag_task is None
    asyncio.run(scenario())


def test_external_ask_cancellation_propagates_and_releases_operation_lock(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        started = asyncio.Event()
        async def block():
            started.set()
            await asyncio.sleep(180)
        h.rag.before = block
        c = h.controller()
        await c.startup()
        task = asyncio.create_task(c.ask_memory(query=PRIVATE))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        h.rag.before = None
        await c.ask_memory(query='next')
        await c.shutdown()
    asyncio.run(scenario())


def test_recording_and_pending_save_prevent_questions(tmp_path):
    async def scenario():
        h = Harness(tmp_path, commit_failures=(False, True))
        c = h.controller()
        await c.startup()
        await c.start_recording(device_id=3)
        with pytest.raises(DesktopStateError):
            await c.ask_memory(query='question')
        result = await c.stop_and_process()
        assert result.save_pending
        with pytest.raises(DesktopStateError):
            await c.ask_memory(query='question')
        assert h.rag.requests == []
        await c.shutdown()
    asyncio.run(scenario())


def test_transcription_and_save_block_questions_without_breaking_voice_flow(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        original = h.provider.transcribe
        async def transcribe(request):
            started.set()
            await release.wait()
            return await original(request)
        h.provider.transcribe = transcribe
        c = h.controller()
        await c.startup()
        await c.start_recording(device_id=3)
        task = asyncio.create_task(c.stop_and_process())
        await started.wait()
        with pytest.raises(DesktopStateError):
            await c.ask_memory(query='question')
        assert h.rag.requests == []
        release.set()
        result = await task
        assert result.memory_id is not None and not result.save_pending
        assert len(h.provider.requests) == len(h.repository.create_or_get_calls) == len(h.indexer.calls) == 1
        await c.ask_memory(query='now allowed')
        await c.shutdown()
    asyncio.run(scenario())


def test_qwen_initialization_failure_closes_previously_created_resources(tmp_path, monkeypatch):
    async def scenario():
        h = Harness(tmp_path)
        c = h.controller()
        c._rag_runtime_factory = rag_runtime.MemoryRagRuntime
        def fail(**kwargs):
            raise RuntimeError(PRIVATE)
        monkeypatch.setattr(rag_runtime, 'create_llm_provider', fail)
        with pytest.raises(DesktopRagConfigurationError) as caught:
            await c.startup()
        assert str(caught.value) == DesktopRagConfigurationError.safe_message
        assert h.embedding.closed == h.indexer.closed == h.engine.dispose_count == h.recorder.shutdown_count == 1
        assert c.provider is None
    asyncio.run(scenario())


def test_actual_runtime_and_indexer_borrow_same_bge_and_reuse_one_qwen(tmp_path, monkeypatch):
    async def scenario():
        h = Harness(tmp_path)
        llm = FakeLLM()
        creations = []
        def llm_factory(**kwargs):
            creations.append(1)
            return llm
        monkeypatch.setattr(rag_runtime, 'create_llm_provider', llm_factory)
        def unexpected_bge(**kwargs):
            pytest.fail('RAG must borrow the Desktop BGE')
        monkeypatch.setattr(rag_runtime, 'create_embedding_provider', unexpected_bge)
        async def retrieve(**kwargs):
            # The real retrieval service and indexer hold the exact same provider.
            await kwargs['service'].prepare(query=kwargs['query'], top_k=kwargs['top_k'])
            return evidence('Venus')
        monkeypatch.setattr(rag_runtime, 'retrieve_memories', retrieve)
        c = h.controller()
        c._rag_runtime_factory = rag_runtime.MemoryRagRuntime
        await c.startup()
        assert llm.calls == []  # creating client never generates / cold-loads Qwen
        indexer = AutomaticMemoryIndexer(settings=Settings(_env_file=None, kulai_vector_dimension=1024), session_factory=h.sessions,
                                        provider=h.embedding, provider_factory=unexpected_bge)
        assert indexer.provider is h.embedding
        await indexer._service.prepare(memory=evidence('indexed Memory').hits[0].memory)
        for _ in range(2):
            assert (await c.ask_memory(query='Where?')).status is DesktopRagStatus.ANSWERED
        await indexer.aclose()
        assert h.embedding.closed == 0
        await c.shutdown()
        assert h.embedding.closed == llm.closed == len(creations) == 1
        assert len(h.embedding.requests) == 3 and len(llm.calls) == 2
    asyncio.run(scenario())


def test_actual_empty_retrieval_skips_qwen_and_generating_progress(tmp_path, monkeypatch):
    async def scenario():
        h = Harness(tmp_path)
        llm = FakeLLM()
        monkeypatch.setattr(rag_runtime, 'create_llm_provider', lambda **kwargs: llm)
        async def empty(**kwargs):
            return evidence()
        monkeypatch.setattr(rag_runtime, 'retrieve_memories', empty)
        c = h.controller()
        c._rag_runtime_factory = rag_runtime.MemoryRagRuntime
        await c.startup()
        result = await c.ask_memory(query='question')
        assert result.status is DesktopRagStatus.INSUFFICIENT_CONTEXT and result.citations == ()
        assert llm.calls == []
        assert [p.state for p in h.progress] == [DesktopRagProgressState.RETRIEVING]
        await c.shutdown()
        assert h.embedding.closed == llm.closed == 1
    asyncio.run(scenario())
