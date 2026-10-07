from __future__ import annotations

import asyncio
import ast
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from kulai_embeddings import (
    EmbeddingCapabilities,
    EmbeddingProviderError,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingVector,
)
from kulai_vector_store import (
    VectorMetric,
    VectorStoreCapabilities,
    VectorStoreProviderError,
    VectorUpsertRequest,
    VectorUpsertResult,
)

from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE,
    Memory,
    MemoryIndexingError,
    MemoryIndexingService,
)
from kulai_memory import indexing_persistence

CONTENT_SENTINEL = "PRIVATE_MEMORY_INDEXING_SENTINEL"
VECTOR_SENTINEL = 12345.678901


def _response(dimension: int = 1024, value: float = VECTOR_SENTINEL):
    return EmbeddingResponse(
        provider_id="fake-embeddings",
        model_id="fake-model:exact-tag",
        dimension=dimension,
        embeddings=(EmbeddingVector(values=(value,) * dimension),),
    )


class FakeProvider:
    provider_id = "fake-embeddings"
    capabilities = EmbeddingCapabilities()

    def __init__(self, response=None, error=None):
        self.response = response or _response()
        self.error = error
        self.requests: list[EmbeddingRequest] = []

    async def embed(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeStore:
    store_id = "fake-store"
    capabilities = VectorStoreCapabilities(
        supports_namespaces=True,
        supports_metadata=True,
        supported_metrics=(VectorMetric.COSINE,),
    )

    def __init__(self, error=None):
        self.error = error
        self.requests: list[VectorUpsertRequest] = []
        self.rows = {}

    async def upsert(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        for record in request.records:
            self.rows[(request.namespace, record.id)] = record
        return VectorUpsertResult(
            store_id=self.store_id,
            ids=tuple(record.id for record in request.records),
            upserted_count=len(request.records),
        )


async def _index(service, memory, store):
    prepared = await service.prepare(memory=memory)
    return await service.upsert(store=store, request=prepared)


def test_index_exact_content_identity_vector_and_allowlisted_metadata():
    memory = Memory(
        content=f"  {CONTENT_SENTINEL}  ",
        metadata={"private": CONTENT_SENTINEL},
    )
    snapshot = memory.model_dump()
    provider = FakeProvider()
    store = FakeStore()
    service = MemoryIndexingService(provider=provider, expected_dimension=1024)

    result = asyncio.run(_index(service, memory, store))

    assert result.ids == (str(memory.id),)
    assert result.upserted_count == 1
    assert len(provider.requests) == 1
    embedding_request = provider.requests[0]
    assert embedding_request.inputs == (memory.content,)
    assert embedding_request.model_hint is None
    assert embedding_request.purpose is None
    assert embedding_request.model_dump() == {
        "inputs": (memory.content,), "model_hint": None, "purpose": None
    }
    request = store.requests[0]
    assert request.namespace == "kulai_memory.memories.v1" == MEMORY_VECTOR_NAMESPACE
    record = request.records[0]
    assert record.id == str(memory.id)
    assert record.vector == provider.response.embeddings[0]
    assert record.metadata == {
        "source_memory_id": str(memory.id),
        "embedding_provider_id": "fake-embeddings",
        "embedding_model_tag": "fake-model:exact-tag",
        "embedding_dimension": 1024,
    }
    assert CONTENT_SENTINEL not in str(record.metadata)
    assert memory.model_dump() == snapshot


def test_reindex_same_memory_updates_one_identity_without_modifying_memory():
    memory = Memory(content="synthetic indexing memory")
    snapshot = memory.model_dump()
    provider = FakeProvider(_response(value=1.0))
    store = FakeStore()
    service = MemoryIndexingService(provider=provider, expected_dimension=1024)

    async def run():
        first = await _index(service, memory, store)
        provider.response = _response(value=2.0).model_copy(
            update={"model_id": "fake-model:updated-tag"}
        )
        second = await _index(service, memory, store)
        return first, second

    first, second = asyncio.run(run())

    assert first.ids == second.ids == (str(memory.id),)
    assert len(store.rows) == 1
    record = store.rows[(MEMORY_VECTOR_NAMESPACE, str(memory.id))]
    assert record.vector.values == (2.0,) * 1024
    assert record.metadata["embedding_model_tag"] == "fake-model:updated-tag"
    assert memory.model_dump() == snapshot


def test_different_memory_ids_with_same_content_have_separate_vector_identities():
    first = Memory(content="same synthetic content")
    second = Memory(content=first.content)
    store = FakeStore()
    service = MemoryIndexingService(provider=FakeProvider(), expected_dimension=1024)

    async def run():
        await _index(service, first, store)
        await _index(service, second, store)

    asyncio.run(run())

    assert set(store.rows) == {
        (MEMORY_VECTOR_NAMESPACE, str(first.id)),
        (MEMORY_VECTOR_NAMESPACE, str(second.id)),
    }


@pytest.mark.parametrize("dimension", [1, 768, 1023, 1025])
def test_dimension_mismatch_never_calls_vector_store(dimension):
    store = FakeStore()
    service = MemoryIndexingService(
        provider=FakeProvider(_response(dimension)), expected_dimension=1024
    )
    with pytest.raises(MemoryIndexingError):
        asyncio.run(_index(service, Memory(content=CONTENT_SENTINEL), store))
    assert store.requests == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"{CONTENT_SENTINEL} {VECTOR_SENTINEL}"),
        EmbeddingProviderError(f"{CONTENT_SENTINEL} {VECTOR_SENTINEL}"),
    ],
)
def test_embedding_failure_has_safe_error_and_no_store_calls(error, caplog, capsys):
    memory = Memory(content=CONTENT_SENTINEL)
    snapshot = memory.model_dump()
    store = FakeStore()
    service = MemoryIndexingService(provider=FakeProvider(error=error), expected_dimension=1024)
    with pytest.raises(MemoryIndexingError) as caught:
        asyncio.run(_index(service, memory, store))
    _assert_safe_error(caught.value, caplog, capsys)
    assert store.requests == []
    assert memory.model_dump() == snapshot


@pytest.mark.parametrize("values", [(), (float("nan"),), (float("inf"),)])
def test_invalid_embedding_response_never_calls_store(values):
    invalid = EmbeddingResponse.model_construct(
        provider_id="fake-embeddings",
        model_id="fake-model:exact-tag",
        dimension=1024,
        embeddings=(EmbeddingVector.model_construct(values=values),),
    )
    store = FakeStore()
    service = MemoryIndexingService(provider=FakeProvider(invalid), expected_dimension=1024)
    with pytest.raises(MemoryIndexingError):
        asyncio.run(_index(service, Memory(content=CONTENT_SENTINEL), store))
    assert store.requests == []


def test_missing_model_identity_is_rejected_before_store():
    response = _response().model_copy(update={"model_id": None})
    store = FakeStore()
    service = MemoryIndexingService(provider=FakeProvider(response), expected_dimension=1024)
    with pytest.raises(MemoryIndexingError):
        asyncio.run(_index(service, Memory(content=CONTENT_SENTINEL), store))
    assert store.requests == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"{CONTENT_SENTINEL} {VECTOR_SENTINEL}"),
        VectorStoreProviderError(f"{CONTENT_SENTINEL} {VECTOR_SENTINEL}"),
    ],
)
def test_store_failure_preserves_memory_and_has_safe_error(error, caplog, capsys):
    memory = Memory(content=CONTENT_SENTINEL)
    snapshot = memory.model_dump()
    service = MemoryIndexingService(provider=FakeProvider(), expected_dimension=1024)
    store = FakeStore(error=error)
    with pytest.raises(MemoryIndexingError) as caught:
        asyncio.run(_index(service, memory, store))
    _assert_safe_error(caught.value, caplog, capsys)
    assert memory.model_dump() == snapshot


def _assert_safe_error(error, caplog, capsys):
    public_text = "".join(traceback.format_exception(error))
    public_text += str(error) + repr(error) + caplog.text
    output = capsys.readouterr()
    public_text += output.out + output.err
    assert CONTENT_SENTINEL not in public_text
    assert str(VECTOR_SENTINEL) not in public_text
    assert error.__suppress_context__ is True
    assert error.__cause__ is None


def test_prepared_request_can_retry_without_another_embedding_call():
    provider = FakeProvider()
    service = MemoryIndexingService(provider=provider, expected_dimension=1024)
    memory = Memory(content="synthetic retry memory")
    store = FakeStore(error=RuntimeError("temporary failure"))

    async def run():
        request = await service.prepare(memory=memory)
        with pytest.raises(MemoryIndexingError):
            await service.upsert(store=store, request=request)
        store.error = None
        return await service.upsert(store=store, request=request)

    result = asyncio.run(run())
    assert result.ids == (str(memory.id),)
    assert len(provider.requests) == 1
    assert len(store.requests) == 2


def test_cancellation_is_propagated_without_store_calls():
    store = FakeStore()
    service = MemoryIndexingService(
        provider=FakeProvider(error=asyncio.CancelledError()), expected_dimension=1024
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_index(service, Memory(content="synthetic cancellation"), store))
    assert store.requests == []


def test_indexing_service_has_no_transaction_methods_or_concrete_dependencies():
    path = Path(__file__).resolve().parents[1] / "src/kulai_memory/application/indexing.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls & {"commit", "rollback", "begin", "close", "aclose"}
    imports = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imports & {
        "kulai_provider_ollama_embeddings", "kulai_vector_store_pgvector", "sqlalchemy"
    }


@pytest.mark.parametrize("commit_fails", [False, True])
def test_host_opens_session_after_embedding_and_handles_commit_safely(
    commit_fails, monkeypatch, caplog, capsys
):
    operations = []
    memory = Memory(content=CONTENT_SENTINEL)
    snapshot = memory.model_dump()

    class Provider(FakeProvider):
        async def embed(self, request):
            assert operations == []
            operations.append("embed")
            return await super().embed(request)

    class Session:
        async def __aenter__(self):
            operations.append("session.open")
            return self

        async def __aexit__(self, *args):
            operations.append("session.close")

        async def scalar(self, statement):
            from sqlalchemy.dialects import postgresql

            compiled = statement.compile(dialect=postgresql.dialect())
            assert "FOR UPDATE" in str(compiled)
            assert memory.id in compiled.params.values()
            operations.append("lock")
            return memory.id

        @asynccontextmanager
        async def begin(self):
            operations.append("begin")
            try:
                yield
                operations.append("commit")
                if commit_fails:
                    raise RuntimeError(f"{CONTENT_SENTINEL} {VECTOR_SENTINEL}")
            except BaseException:
                operations.append("rollback")
                raise

    class Store(FakeStore):
        async def upsert(self, request):
            operations.append("upsert")
            return await super().upsert(request)

    def create_store(*, db, config):
        assert isinstance(db, Session)
        assert config.dimension == 1024
        return Store()

    monkeypatch.setattr(indexing_persistence, "PgVectorStore", create_store)
    provider = Provider()
    service = MemoryIndexingService(provider=provider, expected_dimension=1024)

    def run():
        return asyncio.run(indexing_persistence.index_memory(
            memory=memory, service=service, session_factory=Session
        ))

    if commit_fails:
        with pytest.raises(MemoryIndexingError) as caught:
            run()
        _assert_safe_error(caught.value, caplog, capsys)
    else:
        assert run().ids == (str(memory.id),)

    expected = ["embed", "session.open", "begin", "lock", "upsert", "commit"]
    if commit_fails:
        expected.append("rollback")
    assert operations == expected + ["session.close"]
    assert memory.model_dump() == snapshot


def test_missing_canonical_memory_blocks_prepared_vector_before_store_creation(monkeypatch):
    operations = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def begin(self):
            try:
                yield
            except BaseException:
                operations.append("rollback")
                raise

        async def scalar(self, statement):
            operations.append("lock")
            return None

    def unexpected_store(**kwargs):
        operations.append("store")
        raise AssertionError("Missing Memory must not write a vector")

    monkeypatch.setattr(indexing_persistence, "PgVectorStore", unexpected_store)
    service = MemoryIndexingService(provider=FakeProvider(), expected_dimension=1024)

    async def run():
        prepared = await service.prepare(memory=Memory(content="synthetic stale memory"))
        await indexing_persistence.save_prepared_memory_vector(
            service=service, request=prepared, session_factory=Session,
        )
    with pytest.raises(MemoryIndexingError):
        asyncio.run(run())
    assert operations == ["lock", "rollback"]
