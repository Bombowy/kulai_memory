from __future__ import annotations

import asyncio
import traceback
from uuid import UUID

import pytest
from sqlalchemy.exc import StatementError
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_vector_store import (
    MetadataFilter, VectorMetric, VectorRecord, VectorSearchMatch,
    VectorSearchResult, VectorStoreCapabilities,
)

from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE, Memory, MemoryRetrievalError, MemoryRetrievalService,
)
from kulai_memory import retrieval_persistence as adapter

MODEL = "bge-m3:567m-fp16"
PRIVATE = "PRIVATE_QUERY_MEMORY_PROVIDER_SENTINEL"
VECTOR_VALUE = 12345.678901


class Provider:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities()

    def __init__(self):
        self.requests = []
        self.dimension = 1024
        self.value = VECTOR_VALUE
        self.model = MODEL
        self.failure = None
        self.before_embed = lambda: None

    async def embed(self, request):
        self.before_embed()
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return EmbeddingResponse(
            provider_id=self.provider_id, model_id=self.model, dimension=self.dimension,
            embeddings=(EmbeddingVector(values=(self.value,) * self.dimension),),
        )


def metadata(memory):
    return {"source_memory_id": str(memory.id), "embedding_provider_id": "ollama",
            "embedding_model_tag": MODEL, "embedding_dimension": 1024, "revision": memory.revision,
            "content": "PRIVATE_VECTOR_METADATA_SENTINEL"}


def match(memory, score, **overrides):
    values = metadata(memory)
    values.update(overrides)
    return VectorSearchMatch(record=VectorRecord(
        id=str(memory.id), vector=EmbeddingVector(values=(VECTOR_VALUE,) * 1024), metadata=values,
    ), score=score)


class Store:
    store_id = "fake-vectors"

    def __init__(self, matches=(), metric=VectorMetric.COSINE):
        self.metric = metric
        self.capabilities = VectorStoreCapabilities(
            supports_namespaces=True, supports_metadata=True, supported_metrics=(metric,),
        )
        self.rows = {MEMORY_VECTOR_NAMESPACE: matches}
        self.requests = []
        self.failure = None

    async def search(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return VectorSearchResult(
            store_id=self.store_id, metric=self.metric,
            matches=self.rows.get(request.namespace, ())[:request.top_k],
        )

    async def upsert(self, request):
        pytest.fail("Retrieval cannot upsert")

    async def delete(self, request):
        pytest.fail("Retrieval cannot delete")


class Repository:
    def __init__(self, memories=()):
        self.rows = {memory.id: memory for memory in memories}
        self.read_ids = []
        self.failure = None

    async def get_by_id(self, memory_id):
        self.read_ids.append(memory_id)
        if self.failure is not None:
            raise self.failure
        return self.rows.get(memory_id)


@pytest.fixture
def setup():
    provider = Provider()
    memories = tuple(Memory(id=UUID(int=i), content=f"{PRIVATE}:{i}") for i in (1, 2, 3))
    store = Store(tuple(match(memory, score) for memory, score in zip(
        (memories[0], memories[2], memories[1]), (0.9, 0.8, 0.7),
    )))
    repo = Repository(memories)
    service = MemoryRetrievalService(
        provider=provider, expected_dimension=1024,
        expected_provider_id="ollama", expected_model_tag=MODEL,
    )
    return provider, service, store, repo, memories


async def retrieve(setup, query=" query with whitespace ", top_k=5):
    provider, service, store, repo, memories = setup
    request = await service.prepare(query=query, top_k=top_k)
    return await service.search(store=store, repository=repo, request=request)


@pytest.mark.parametrize("query", ["", " \t\n", None, 42, True, b"text", [], "x" * 10_001])
def test_invalid_query_has_no_provider_or_search_calls(query, setup):
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup, query=query))
    assert caught.value.code == "retrieval.invalid_query"
    assert setup[0].requests == setup[2].requests == []


@pytest.mark.parametrize("top_k", [0, 21, True, False, 1.5, "5", None])
def test_invalid_top_k_has_no_provider_or_search_calls(top_k, setup):
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup, top_k=top_k))
    assert caught.value.code == "retrieval.invalid_top_k"
    assert setup[0].requests == setup[2].requests == []


@pytest.mark.parametrize("top_k", [1, 5, 20])
def test_exact_query_namespace_top_k_and_order(top_k, setup):
    query = "  original query\n "
    result = asyncio.run(retrieve(setup, query=query, top_k=top_k))
    provider, service, store, repo, memories = setup
    assert provider.requests[0].inputs == (query,)
    assert provider.requests[0].model_hint is None and provider.requests[0].purpose is None
    assert store.requests[0].namespace == "kulai_memory.memories.v1"
    assert store.requests[0].top_k == top_k and store.requests[0].filters == ()
    expected = (memories[0], memories[2], memories[1])[:top_k]
    assert tuple(hit.memory for hit in result.hits) == expected
    assert repo.read_ids == [memory.id for memory in expected]
    assert tuple(hit.rank for hit in result.hits) == tuple(range(1, len(expected) + 1))
    assert tuple(hit.score for hit in result.hits) == (0.9, 0.8, 0.7)[:top_k]
    assert tuple(hit.vector_record_id for hit in result.hits) == tuple(str(m.id) for m in expected)
    assert result.metric == VectorMetric.COSINE
    public = result.model_dump_json()
    assert str(VECTOR_VALUE) not in public
    assert "PRIVATE_VECTOR_METADATA_SENTINEL" not in public
    assert '"vector":' not in public


def test_max_query_length_is_accepted_without_changes(setup):
    asyncio.run(retrieve(setup, query="x" * 10_000))
    assert setup[0].requests[0].inputs == ("x" * 10_000,)


@pytest.mark.parametrize("failure", ["dimension", "model", "provider", "exception", "zero", "empty", "nan", "inf"])
def test_embedding_failure_stops_before_search_and_hides_values(failure, setup, caplog):
    provider = setup[0]
    expected_code = "retrieval.embedding_failed"
    if failure == "dimension":
        provider.dimension = 768
        expected_code = "retrieval.dimension_mismatch"
    elif failure == "model":
        provider.model = PRIVATE
        expected_code = "retrieval.embedding_incompatible"
    elif failure == "provider":
        provider.provider_id = PRIVATE
        expected_code = "retrieval.embedding_incompatible"
    elif failure == "exception":
        provider.failure = RuntimeError(f"{PRIVATE} {VECTOR_VALUE}")
    elif failure == "empty":
        provider.dimension = 0
    else:
        provider.value = {"zero": 0.0, "nan": float("nan"), "inf": float("inf")}[failure]
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == expected_code
    assert setup[2].requests == setup[3].read_ids == []
    error = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert PRIVATE not in error and str(VECTOR_VALUE) not in error


def test_other_namespace_cannot_be_returned(setup):
    setup[2].rows["other.namespace"] = (match(setup[4][1], 1.0),)
    result = asyncio.run(retrieve(setup, top_k=1))
    assert result.hits[0].memory == setup[4][0]


def test_missing_source_memory_id_is_incompatible(setup):
    row = match(setup[4][0], 0.9)
    data = dict(row.record.metadata)
    data.pop("source_memory_id")
    setup[2].rows[MEMORY_VECTOR_NAMESPACE] = (
        row.model_copy(update={"record": row.record.model_copy(update={"metadata": data})}),
    )
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == "retrieval.incompatible_metadata"


@pytest.mark.parametrize("revision", [None, True, 1.0, 2, 0])
def test_missing_invalid_or_stale_revision_is_incompatible(setup, revision):
    original = match(setup[4][0], 0.9)
    data = dict(original.record.metadata)
    if revision is None:
        data.pop("revision")
    else:
        data["revision"] = revision
    setup[2].rows[MEMORY_VECTOR_NAMESPACE] = (
        original.model_copy(update={"record": original.record.model_copy(update={"metadata": data})}),
    )
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == "retrieval.incompatible_metadata"


def test_archived_canonical_cannot_be_exposed_even_with_a_vector(setup):
    from datetime import UTC, datetime
    memory = setup[4][0]
    setup[3].rows[memory.id] = memory.model_copy(update={"archived_at": datetime.now(UTC)})
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == "retrieval.memory_archived"


@pytest.mark.parametrize("field,value", [
    ("embedding_provider_id", "different"), ("embedding_model_tag", "different:model"),
    ("embedding_dimension", 768), ("embedding_dimension", 1024.0),
    ("embedding_dimension", True), ("source_memory_id", str(UUID(int=99))),
])
def test_any_incompatible_hit_fails_entire_result_before_lookup(field, value, setup):
    store, memories = setup[2], setup[4]
    store.rows[MEMORY_VECTOR_NAMESPACE] = (match(memories[0], 0.9), match(memories[1], 0.8, **{field: value}))
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == "retrieval.incompatible_metadata"
    assert setup[3].read_ids == []


def test_missing_embedding_metadata_fails(setup):
    record = setup[2].rows[MEMORY_VECTOR_NAMESPACE][0].record
    values = dict(record.metadata)
    del values["embedding_provider_id"]
    setup[2].rows[MEMORY_VECTOR_NAMESPACE] = (VectorSearchMatch(
        record=VectorRecord(id=record.id, vector=record.vector, metadata=values), score=0.9,
    ),)
    with pytest.raises(MemoryRetrievalError, match="incompatible"):
        asyncio.run(retrieve(setup))


def test_orphan_is_a_controlled_integrity_error(setup):
    del setup[3].rows[setup[4][0].id]
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == "retrieval.vector_orphan"


@pytest.mark.parametrize("record_id", [PRIVATE, UUID(int=1).hex])
def test_invalid_or_noncanonical_uuid_is_safe(record_id, setup):
    original = setup[2].rows[MEMORY_VECTOR_NAMESPACE][0]
    setup[2].rows[MEMORY_VECTOR_NAMESPACE] = (VectorSearchMatch(record=VectorRecord(
        id=record_id, vector=original.record.vector, metadata=original.record.metadata,
    ), score=0.9),)
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    assert caught.value.code == "retrieval.invalid_record_id"
    assert PRIVATE not in str(caught.value)
    assert setup[3].read_ids == []


@pytest.mark.parametrize("stage", ["store", "repository"])
def test_infrastructure_failures_hide_private_content_and_vector(stage, setup, caplog):
    target = setup[2] if stage == "store" else setup[3]
    target.failure = RuntimeError(f"{PRIVATE} {VECTOR_VALUE}")
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    error = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert caught.value.code == "retrieval.operation_failed"
    assert PRIVATE not in error and str(VECTOR_VALUE) not in error


def test_sql_bind_failure_never_exposes_parameters_or_vector_values(setup, caplog):
    setup[2].failure = StatementError(
        PRIVATE, "synthetic query statement", {"embedding": (VECTOR_VALUE,) * 1024},
        ValueError(PRIVATE),
    )
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(retrieve(setup))
    public = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert PRIVATE not in public and str(VECTOR_VALUE) not in public
    assert setup[3].read_ids == []


def test_wrong_repository_identity_is_rejected(setup):
    setup[3].rows[setup[4][0].id] = setup[4][1]
    with pytest.raises(MemoryRetrievalError):
        asyncio.run(retrieve(setup))


def test_empty_vector_matches_returns_empty_result(setup):
    setup[2].rows = {}
    result = asyncio.run(retrieve(setup))
    assert result.hits == () and setup[3].read_ids == []


def test_wrong_metric_is_rejected(setup):
    provider, service, store, repo, memories = setup
    store = Store(metric=VectorMetric.EUCLIDEAN)
    async def run():
        request = await service.prepare(query="synthetic")
        return await service.search(store=store, repository=repo, request=request)
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(run())
    assert caught.value.code == "retrieval.unsupported_metric"


@pytest.mark.parametrize("update", [
    {"namespace": None}, {"namespace": "other"}, {"top_k": True}, {"top_k": 21},
    {"filters": (MetadataFilter.eq("embedding_provider_id", "ollama"),)},
])
def test_prepared_request_cannot_bypass_policy(update, setup):
    async def run():
        request = await setup[1].prepare(query="synthetic")
        return await setup[1].search(
            store=setup[2], repository=setup[3], request=request.model_copy(update=update),
        )
    with pytest.raises(MemoryRetrievalError):
        asyncio.run(run())
    assert setup[2].requests == []


class SessionFactory:
    def __init__(self, *, failure=False):
        self.calls = 0
        self.active = False
        self.events = []
        self.failure = failure

    def __call__(self):
        self.calls += 1
        factory = self
        class Session:
            async def __aenter__(self):
                factory.active = True
                factory.events.append("open")
                return self
            async def execute(self, statement):
                assert str(statement) == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                factory.events.append("read-only")
                if factory.failure:
                    raise RuntimeError(PRIVATE)
            async def rollback(self):
                factory.events.append("rollback")
            async def commit(self):
                pytest.fail("Retrieval cannot commit")
            async def __aexit__(self, *args):
                factory.active = False
                factory.events.append("close")
        return Session()


def test_host_embeds_before_session_and_rolls_back_on_success(setup, monkeypatch):
    factory = SessionFactory()
    setup[0].before_embed = lambda: factory.events.append("embed") if not factory.active else pytest.fail("Open connection")
    def store(*, db, config):
        assert factory.active and config.dimension == 1024 and config.metric == VectorMetric.COSINE
        return setup[2]
    monkeypatch.setattr(adapter, "PgVectorStore", store)
    monkeypatch.setattr(adapter, "PostgresMemoryRepository", lambda **kwargs: setup[3])
    result = asyncio.run(adapter.retrieve_memories(
        query="synthetic", service=setup[1], session_factory=factory,
    ))
    assert len(result.hits) == 3
    assert factory.events == ["embed", "open", "read-only", "rollback", "close"]


def test_host_provider_failure_never_creates_db_session(setup):
    factory = SessionFactory()
    setup[0].failure = RuntimeError(PRIVATE)
    with pytest.raises(MemoryRetrievalError):
        asyncio.run(adapter.retrieve_memories(query="synthetic", service=setup[1], session_factory=factory))
    assert factory.calls == 0


def test_host_rejects_noncanonical_dimension_before_session(setup):
    provider = setup[0]
    provider.dimension = 768
    service = MemoryRetrievalService(
        provider=provider, expected_dimension=768,
        expected_provider_id="ollama", expected_model_tag=MODEL,
    )
    factory = SessionFactory()
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(adapter.retrieve_memories(query="synthetic", service=service, session_factory=factory))
    assert caught.value.code == "retrieval.dimension_mismatch" and factory.calls == 0


def test_host_db_failure_rolls_back_closes_and_is_safe(setup):
    factory = SessionFactory(failure=True)
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(adapter.retrieve_memories(query="synthetic", service=setup[1], session_factory=factory))
    assert factory.events == ["open", "read-only", "rollback", "close"]
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))


def test_host_integrity_failure_also_rolls_back(setup, monkeypatch):
    factory = SessionFactory()
    del setup[3].rows[setup[4][0].id]
    monkeypatch.setattr(adapter, "PgVectorStore", lambda **kwargs: setup[2])
    monkeypatch.setattr(adapter, "PostgresMemoryRepository", lambda **kwargs: setup[3])
    with pytest.raises(MemoryRetrievalError) as caught:
        asyncio.run(adapter.retrieve_memories(query="synthetic", service=setup[1], session_factory=factory))
    assert caught.value.code == "retrieval.vector_orphan"
    assert factory.events[-2:] == ["rollback", "close"]


def test_cancellation_closes_read_session_without_commit(setup, monkeypatch):
    factory = SessionFactory()
    class CancelledStore(Store):
        async def search(self, request):
            raise asyncio.CancelledError
    monkeypatch.setattr(adapter, "PgVectorStore", lambda **kwargs: CancelledStore())
    monkeypatch.setattr(adapter, "PostgresMemoryRepository", lambda **kwargs: setup[3])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.retrieve_memories(query="synthetic", service=setup[1], session_factory=factory))
    assert factory.events == ["open", "read-only", "rollback", "close"]
