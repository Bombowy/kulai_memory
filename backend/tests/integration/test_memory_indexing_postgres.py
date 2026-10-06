from __future__ import annotations

import asyncio
import os
import traceback

import pytest
from kulai_embeddings import (
    EmbeddingCapabilities,
    EmbeddingResponse,
    EmbeddingVector,
)
from kulai_provider_ollama_embeddings import provider as ollama_provider_module
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.tests.integration.test_memory_postgres import (
    _owned_migrated_session_factory,
    _require_opt_in,
)
from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE,
    MemoryIndexingError,
    MemoryIndexingService,
    MemoryService,
)
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.indexing_persistence import index_memory, save_prepared_memory_vector
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.settings import Settings


class SyntheticProvider:
    provider_id = "synthetic-embeddings"
    capabilities = EmbeddingCapabilities()

    def __init__(self, engine):
        self.engine = engine
        self.calls = 0

    async def embed(self, request):
        # A Memory read session has closed and no vector write session exists.
        assert self.engine.pool.checkedout() == 0
        assert request.inputs == ("synthetic vector indexing memory",)
        assert request.model_hint is None and request.purpose is None
        self.calls += 1
        return EmbeddingResponse(
            provider_id=self.provider_id,
            model_id=f"synthetic-model:revision-{self.calls}",
            dimension=1024,
            embeddings=(EmbeddingVector(values=(float(self.calls),) * 1024),),
        )


async def _create_synthetic_memory(factory):
    async with factory() as session:
        service = MemoryService(repository=PostgresMemoryRepository(db=session))
        memory = await service.create_memory(
            content="synthetic vector indexing memory",
            metadata={"private_note": "ONLY_IN_MEMORY_SENTINEL"},
        )
        await session.commit()
    # New read-only session yields a detached canonical Memory.
    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        canonical = await PostgresMemoryRepository(db=session).get_by_id(memory.id)
        await session.rollback()
    assert canonical is not None
    return canonical


async def _read_back(factory, memory):
    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        memories = await session.scalar(text("SELECT count(*) FROM memories"))
        vectors = await session.scalar(text("SELECT count(*) FROM kulai_vector_records"))
        stored = await PostgresMemoryRepository(db=session).get_by_id(memory.id)
        rows = (await session.execute(text(
            "SELECT pk, namespace_key, record_id, vector_dims(embedding) AS dimension, "
            "metadata_json, md5(embedding::text) AS vector_hash "
            "FROM kulai_vector_records"
        ))).mappings().all()
        await session.rollback()
    assert {"memories": memories, "memory_unchanged": stored == memory} == {
        "memories": 1, "memory_unchanged": True
    }
    return vectors, rows


def _assert_vector(rows, memory, *, provider, model):
    assert len(rows) == 1
    row = rows[0]
    assert row["namespace_key"] == "kulai_memory.memories.v1" == MEMORY_VECTOR_NAMESPACE
    assert row["record_id"] == str(memory.id)
    assert row["dimension"] == 1024
    assert row["metadata_json"] == {
        "source_memory_id": str(memory.id),
        "embedding_provider_id": provider,
        "embedding_model_tag": model,
        "embedding_dimension": 1024,
    }
    return row


async def _synthetic_reindex():
    async with _owned_migrated_session_factory() as factory:
        memory = await _create_synthetic_memory(factory)
        provider = SyntheticProvider(factory.kw["bind"])
        service = MemoryIndexingService(provider=provider, expected_dimension=1024)
        first = await index_memory(memory=memory, service=service, session_factory=factory)
        count, rows = await _read_back(factory, memory)
        assert count == 1
        first_row = _assert_vector(rows, memory, provider=provider.provider_id,
                                   model="synthetic-model:revision-1")
        second = await index_memory(memory=memory, service=service, session_factory=factory)
        count, rows = await _read_back(factory, memory)
        assert count == 1
        second_row = _assert_vector(rows, memory, provider=provider.provider_id,
                                    model="synthetic-model:revision-2")
        assert first.ids == second.ids == (str(memory.id),)
        assert first_row["pk"] == second_row["pk"]
        assert first_row["vector_hash"] != second_row["vector_hash"]
        assert provider.calls == 2


async def _commit_failure_rolls_back():
    async with _owned_migrated_session_factory() as factory:
        memory = await _create_synthetic_memory(factory)
        provider = SyntheticProvider(factory.kw["bind"])
        service = MemoryIndexingService(provider=provider, expected_dimension=1024)
        prepared = await service.prepare(memory=memory)

        class RejectCommitSession(Session):
            pass

        attempts = {"commit": 0}

        def fail_commit(session):
            attempts["commit"] += 1
            raise RuntimeError("PRIVATE_COMMIT_SENTINEL 12345.678901")

        event.listen(RejectCommitSession, "before_commit", fail_commit)
        failing_factory = async_sessionmaker(
            factory.kw["bind"], sync_session_class=RejectCommitSession,
            expire_on_commit=False,
        )
        try:
            with pytest.raises(MemoryIndexingError) as caught:
                await save_prepared_memory_vector(
                    service=service, request=prepared, session_factory=failing_factory
                )
        finally:
            event.remove(RejectCommitSession, "before_commit", fail_commit)
        public_error = "".join(traceback.format_exception(caught.value))
        assert "PRIVATE_COMMIT_SENTINEL" not in public_error
        assert "12345.678901" not in public_error
        assert attempts["commit"] == 1
        count, rows = await _read_back(factory, memory)
        assert count == 0 and rows == []
        # Retry the prepared vector in a fresh transaction without more STT/embed.
        await save_prepared_memory_vector(
            service=service, request=prepared, session_factory=factory
        )
        count, rows = await _read_back(factory, memory)
        assert count == 1
        _assert_vector(rows, memory, provider=provider.provider_id,
                       model="synthetic-model:revision-1")
        assert provider.calls == 1


async def _real_bge_reindex(monkeypatch):
    async with _owned_migrated_session_factory() as factory:
        memory = await _create_synthetic_memory(factory)
        engine = factory.kw["bind"]
        counts = {"clients": 0, "requests": 0, "closed": 0, "overrides": 0}
        original_client = ollama_provider_module.AsyncClient

        def tracked_client(*args, **kwargs):
            counts["clients"] += 1
            client = original_client(*args, **kwargs)
            original_embed, original_close = client.embed, client.close

            async def tracked_embed(*args, **kwargs):
                assert engine.pool.checkedout() == 0
                counts["requests"] += 1
                if kwargs.get("dimensions") is not None:
                    counts["overrides"] += 1
                return await original_embed(*args, **kwargs)

            async def tracked_close():
                counts["closed"] += 1
                await original_close()

            monkeypatch.setattr(client, "embed", tracked_embed)
            monkeypatch.setattr(client, "close", tracked_close)
            return client

        monkeypatch.setattr(ollama_provider_module, "AsyncClient", tracked_client)
        settings = Settings(
            _env_file=None, kulai_embedding_model="bge-m3:567m-fp16",
            kulai_ollama_base_url="http://127.0.0.1:11434",
            kulai_vector_dimension=1024,
        )
        async with create_embedding_provider(settings=settings) as provider:
            service = MemoryIndexingService(provider=provider, expected_dimension=1024)
            first = await index_memory(memory=memory, service=service, session_factory=factory)
            count, rows = await _read_back(factory, memory)
            assert count == 1
            first_row = _assert_vector(rows, memory, provider="ollama", model="bge-m3:567m-fp16")
            second = await index_memory(memory=memory, service=service, session_factory=factory)
            count, rows = await _read_back(factory, memory)
            assert count == 1
            second_row = _assert_vector(rows, memory, provider="ollama", model="bge-m3:567m-fp16")
            assert first.ids == second.ids == (str(memory.id),)
            assert first_row["pk"] == second_row["pk"]
            assert counts["clients"] == 1
        assert counts == {"clients": 1, "requests": 2, "closed": 1, "overrides": 0}


def test_real_pgvector_reindex_updates_one_durable_row():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_synthetic_reindex(), timeout=120))


def test_real_pgvector_commit_failure_rolls_back_and_preserves_memory():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_commit_failure_rolls_back(), timeout=120))


def test_real_bge_and_pgvector_reindex_one_identity(monkeypatch):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_OLLAMA_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_OLLAMA_INTEGRATION=1 for real BGE + PostgreSQL.")
    asyncio.run(asyncio.wait_for(_real_bge_reindex(monkeypatch), timeout=180))
