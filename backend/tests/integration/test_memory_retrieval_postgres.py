from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_provider_ollama_embeddings import provider as provider_module
from kulai_vector_store import VectorRecord, VectorUpsertRequest
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import event

from backend.tests.integration.test_memory_postgres import (
    _owned_migrated_session_factory, _require_opt_in,
)
from backend.tests.retrieval_golden import (
    GOLDEN_MEMORIES, GOLDEN_QUERIES, GOLDEN_THRESHOLDS,
    dataset_sha256, evaluate_queries,
)
from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE, Memory, MemoryIndexingService,
    MemoryRetrievalError, MemoryRetrievalService,
)
from kulai_memory.backfill import BackfillReader, EMBEDDING_MODEL
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.indexing_persistence import index_memory
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.retrieval_persistence import retrieve_memories
from kulai_memory.settings import Settings


def _metadata(record_id):
    return {"source_memory_id": record_id, "embedding_provider_id": "ollama",
            "embedding_model_tag": EMBEDDING_MODEL, "embedding_dimension": 1024,
            "content": "synthetic vector metadata is not canonical content"}


class QueryProvider:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities()

    def __init__(self, engine):
        self.engine = engine
        self.calls = 0

    async def embed(self, request):
        assert self.engine.pool.checkedout() == 0
        assert request.model_hint is None and request.purpose is None
        self.calls += 1
        return EmbeddingResponse(
            provider_id="ollama", model_id=EMBEDDING_MODEL, dimension=1024,
            embeddings=(EmbeddingVector(values=(1.0,) + (0.0,) * 1023),),
        )


def _service(provider):
    return MemoryRetrievalService(
        provider=provider, expected_dimension=1024,
        expected_provider_id="ollama", expected_model_tag=EMBEDDING_MODEL,
    )


async def _seed_memories(factory, memories):
    async with factory() as session:
        repo = PostgresMemoryRepository(db=session)
        for memory in memories:
            await repo.create(memory)
        await session.commit()


async def _seed_vectors(factory, records, *, namespace=MEMORY_VECTOR_NAMESPACE):
    async with factory() as session:
        store = PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024))
        await store.upsert(VectorUpsertRequest(namespace=namespace, records=tuple(records)))
        await session.commit()


def _observe_reads(engine):
    statements, commits = [], []
    def statement(connection, cursor, sql, parameters, context, executemany):
        # Keep only statement kind, never SQL parameters or vector values.
        kind = sql.lstrip().split(maxsplit=1)[0].upper()
        assert kind in {"SELECT", "SET", "SHOW"}
        statements.append(kind)
    def commit(connection):
        commits.append(True)
    event.listen(engine.sync_engine, "before_cursor_execute", statement)
    event.listen(engine.sync_engine, "commit", commit)
    def remove():
        event.remove(engine.sync_engine, "before_cursor_execute", statement)
        event.remove(engine.sync_engine, "commit", commit)
    return statements, commits, remove


async def _known_vector_search():
    async with _owned_migrated_session_factory() as factory:
        engine = factory.kw["bind"]
        base = datetime(2025, 1, 1, tzinfo=UTC)
        memories = tuple(Memory(id=UUID(int=i), content=f"synthetic canonical Memory {i}",
                                created_at=base + timedelta(seconds=offset))
                         for offset, i in enumerate((101, 202, 303)))
        await _seed_memories(factory, memories)
        vectors = ((1.0, 0.0), (0.6, 0.8), (0.8, 0.6))
        records = tuple(VectorRecord(id=str(memory.id),
                                    vector=EmbeddingVector(values=pair + (0.0,) * 1022),
                                    metadata=_metadata(str(memory.id)))
                        for memory, pair in zip(memories, vectors))
        await _seed_vectors(factory, records)
        # Same record identity with a better cosine score, but a different namespace.
        other = VectorRecord(id=str(memories[1].id),
                             vector=EmbeddingVector(values=(1.0,) + (0.0,) * 1023),
                             metadata=_metadata(str(memories[1].id)))
        await _seed_vectors(factory, (other,), namespace="synthetic.other.namespace")
        reader = BackfillReader(factory)
        before = await reader.fingerprints()
        provider = QueryProvider(engine)
        statements, commits, remove = _observe_reads(engine)
        try:
            result = await retrieve_memories(query="synthetic known query", top_k=2,
                                             service=_service(provider), session_factory=factory)
            all_results = await retrieve_memories(query="synthetic known query", top_k=20,
                                                  service=_service(provider), session_factory=factory)
        finally:
            remove()
        assert tuple(h.memory.id for h in result.hits) == (memories[0].id, memories[2].id)
        assert tuple(h.rank for h in result.hits) == (1, 2)
        assert tuple(h.score for h in result.hits) == pytest.approx((1.0, 0.8))
        assert tuple(h.memory for h in all_results.hits) == (memories[0], memories[2], memories[1])
        assert tuple(h.score for h in all_results.hits) == pytest.approx((1.0, 0.8, 0.6))
        assert provider.calls == 2 and commits == []
        assert "SELECT" in statements and "SET" in statements
        assert await reader.fingerprints() == before


async def _integrity_error(kind):
    async with _owned_migrated_session_factory() as factory:
        memory = Memory(id=UUID(int=401), content="synthetic retrieval integrity memory")
        await _seed_memories(factory, (memory,))
        record_id = str(UUID(int=499)) if kind == "orphan" else str(memory.id)
        values = _metadata(record_id)
        if kind == "metadata":
            values["embedding_model_tag"] = "synthetic:wrong-model"
        await _seed_vectors(factory, (VectorRecord(
            id=record_id, vector=EmbeddingVector(values=(1.0,) + (0.0,) * 1023), metadata=values,
        ),))
        reader = BackfillReader(factory)
        before = await reader.fingerprints()
        with pytest.raises(MemoryRetrievalError) as caught:
            await retrieve_memories(query="synthetic integrity query", service=_service(QueryProvider(factory.kw["bind"])),
                                    session_factory=factory)
        expected = "retrieval.vector_orphan" if kind == "orphan" else "retrieval.incompatible_metadata"
        assert caught.value.code == expected
        assert await reader.fingerprints() == before


async def _golden_retrieval(monkeypatch):
    frozen_hash = dataset_sha256()
    assert frozen_hash == "73489b2a359a444074a7aa191337de00d7dcdeea86c741dd59d6e5c7c285bdf7"
    print("golden.thresholds=Recall@1>=0.60,Recall@3>=0.80,Recall@5>=0.90")
    async with _owned_migrated_session_factory() as factory:
        engine = factory.kw["bind"]
        await _seed_memories(factory, GOLDEN_MEMORIES)
        reader = BackfillReader(factory)
        before_index = await reader.fingerprints()
        counts = {"clients": 0, "requests": 0, "native_1024": 0, "closed": 0, "overrides": 0}
        original_client = provider_module.AsyncClient
        def client(*args, **kwargs):
            counts["clients"] += 1
            instance = original_client(*args, **kwargs)
            original_embed, original_close = instance.embed, instance.close
            async def embed(*args, **kwargs):
                assert engine.pool.checkedout() == 0
                counts["requests"] += 1
                if kwargs.get("dimensions") is not None:
                    counts["overrides"] += 1
                response = await original_embed(*args, **kwargs)
                assert len(response.embeddings) == 1
                assert len(response.embeddings[0]) == 1024
                counts["native_1024"] += 1
                return response
            async def close():
                counts["closed"] += 1
                await original_close()
            monkeypatch.setattr(instance, "embed", embed)
            monkeypatch.setattr(instance, "close", close)
            return instance
        monkeypatch.setattr(provider_module, "AsyncClient", client)
        settings = Settings(_env_file=None, kulai_embedding_model=EMBEDDING_MODEL,
                            kulai_ollama_base_url="http://127.0.0.1:11434", kulai_vector_dimension=1024)
        rankings = {}
        async with create_embedding_provider(settings=settings) as provider:
            indexing = MemoryIndexingService(provider=provider, expected_dimension=1024)
            for memory in GOLDEN_MEMORIES:
                await index_memory(memory=memory, service=indexing, session_factory=factory)
            before_query = await reader.fingerprints()
            assert before_query[0] == before_index[0]
            assert before_query[0].count == before_query[1].count == 16
            retrieval = _service(provider)
            statements, commits, remove = _observe_reads(engine)
            try:
                for query in GOLDEN_QUERIES:
                    result = await retrieve_memories(query=query.text, top_k=5, service=retrieval,
                                                     session_factory=factory)
                    rankings[query.id] = tuple(hit.memory.id for hit in result.hits)
            finally:
                remove()
            assert commits == [] and "SELECT" in statements
            assert await reader.fingerprints() == before_query
        assert counts == {"clients": 1, "requests": 32, "native_1024": 32, "closed": 1, "overrides": 0}
        assert dataset_sha256() == frozen_hash
        metrics = evaluate_queries(GOLDEN_QUERIES, rankings)
        output = {"dataset_sha256": frozen_hash, "documents": 16, "queries": 16,
                  "recall_at_1": metrics.recall_at_1, "recall_at_3": metrics.recall_at_3,
                  "recall_at_5": metrics.recall_at_5, "mrr_at_5": metrics.mrr_at_5,
                  "provider": counts, "fingerprints_unchanged": True}
        for language in ("en", "pl"):
            queries = tuple(q for q in GOLDEN_QUERIES if q.language == language)
            language_metrics = evaluate_queries(queries, {q.id: rankings[q.id] for q in queries})
            output[language] = {"recall_at_1": language_metrics.recall_at_1,
                                "recall_at_3": language_metrics.recall_at_3,
                                "recall_at_5": language_metrics.recall_at_5,
                                "mrr_at_5": language_metrics.mrr_at_5}
        print("golden.evaluation=" + json.dumps(output, sort_keys=True))
        for query in GOLDEN_QUERIES:
            if not query.expected.intersection(rankings[query.id][:1]):
                print("golden.diagnostic=" + json.dumps({
                    "query_id": query.id, "expected_ids": sorted(str(i) for i in query.expected),
                    "ranked_ids": [str(i) for i in rankings[query.id]],
                }, sort_keys=True))
        assert metrics.recall_at_1 >= GOLDEN_THRESHOLDS[1], "Golden Recall@1 failed"
        assert metrics.recall_at_3 >= GOLDEN_THRESHOLDS[3], "Golden Recall@3 failed"
        assert metrics.recall_at_5 >= GOLDEN_THRESHOLDS[5], "Golden Recall@5 failed"


def test_real_pgvector_canonical_ordered_readonly_retrieval():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_known_vector_search(), timeout=120))


@pytest.mark.parametrize("kind", ["orphan", "metadata"])
def test_real_pgvector_integrity_errors_leave_source_unchanged(kind):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_integrity_error(kind), timeout=120))


def test_real_bge_golden_retrieval(monkeypatch):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_OLLAMA_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_OLLAMA_INTEGRATION=1 for real BGE golden retrieval.")
    asyncio.run(asyncio.wait_for(_golden_retrieval(monkeypatch), timeout=300))
