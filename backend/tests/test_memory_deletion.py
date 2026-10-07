from __future__ import annotations

import asyncio
import ast
import traceback
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import uuid4

import pytest
from kulai_vector_store import VectorDeleteResult, VectorMetric, VectorStoreCapabilities

from kulai_memory import deletion_persistence
from kulai_memory.application import (
    MEMORY_VECTOR_NAMESPACE, MemoryDeletionError, MemoryDeletionRepository,
    MemoryDeletionResult, MemoryDeletionService,
)

PRIVATE = "PRIVATE_CONTENT_VECTOR_PASSWORD_SENTINEL 12345.678901"


class Repository:
    def __init__(self, operations, *, present=True, error=None):
        self.operations = operations
        self.present = present
        self.error = error

    async def delete_by_id(self, memory_id):
        self.operations.append(("memory", memory_id))
        if self.error is not None:
            raise self.error
        return self.present


class Store:
    store_id = "synthetic-store"
    capabilities = VectorStoreCapabilities(
        supports_namespaces=True, supported_metrics=(VectorMetric.COSINE,),
    )

    def __init__(self, operations, *, count=1, error=None):
        self.operations = operations
        self.count = count
        self.error = error

    async def delete(self, request):
        self.operations.append(("vector", request))
        if self.error is not None:
            raise self.error
        return VectorDeleteResult(
            store_id=self.store_id, requested_ids=request.ids, deleted_count=self.count,
        )


def _assert_private_error(error, caplog, capsys):
    public = "".join(traceback.format_exception(error)) + caplog.text
    captured = capsys.readouterr()
    public += captured.out + captured.err
    assert PRIVATE not in public
    assert "12345.678901" not in public
    assert str(error) == "Memory deletion could not be completed."


@pytest.mark.parametrize("present,count", [(True, 1), (True, 0), (False, 1), (False, 0)])
def test_delete_exact_identity_in_canonical_then_vector_order(present, count):
    operations = []
    memory_id = uuid4()
    repository = Repository(operations, present=present)
    assert isinstance(repository, MemoryDeletionRepository)
    result = asyncio.run(MemoryDeletionService(
        repository=repository, store=Store(operations, count=count),
    ).delete(memory_id))
    assert result == MemoryDeletionResult(memory_id, present, count)
    assert operations[0] == ("memory", memory_id)
    assert len(operations) == 2 and operations[1][0] == "vector"
    request = operations[1][1]
    assert request.namespace == MEMORY_VECTOR_NAMESPACE == "kulai_memory.memories.v1"
    assert request.ids == (str(memory_id),)
    with pytest.raises(FrozenInstanceError):
        result.memory_deleted = False


def test_repeated_delete_returns_zero_counts_for_same_identity():
    async def run():
        operations = []
        memory_id = uuid4()
        repository, store = Repository(operations), Store(operations)
        service = MemoryDeletionService(repository=repository, store=store)
        assert await service.delete(memory_id) == MemoryDeletionResult(memory_id, True, 1)
        repository.present, store.count = False, 0
        assert await service.delete(memory_id) == MemoryDeletionResult(memory_id, False, 0)
        assert operations[1][1] == operations[3][1]
    asyncio.run(run())


@pytest.mark.parametrize("memory_id", ["not-a-uuid", str(uuid4()), None, True, 123])
def test_invalid_identifier_has_no_side_effects(memory_id):
    operations = []
    service = MemoryDeletionService(repository=Repository(operations), store=Store(operations))
    with pytest.raises(MemoryDeletionError):
        asyncio.run(service.delete(memory_id))
    assert operations == []


def test_invalid_repository_response_blocks_vector_delete():
    operations = []
    with pytest.raises(MemoryDeletionError):
        asyncio.run(MemoryDeletionService(
            repository=Repository(operations, present=1), store=Store(operations),
        ).delete(uuid4()))
    assert len(operations) == 1


@pytest.mark.parametrize("stage", ["memory", "vector"])
def test_service_failure_is_safe(stage, caplog, capsys):
    operations = []
    error = RuntimeError(PRIVATE)
    with pytest.raises(MemoryDeletionError) as caught:
        asyncio.run(MemoryDeletionService(
            repository=Repository(operations, error=error if stage == "memory" else None),
            store=Store(operations, error=error if stage == "vector" else None),
        ).delete(uuid4()))
    assert len(operations) == (1 if stage == "memory" else 2)
    _assert_private_error(caught.value, caplog, capsys)


@pytest.mark.parametrize("stage", ["success", "memory", "vector", "commit", "cancel_memory", "cancel_vector"])
def test_host_shares_session_and_returns_after_commit_or_rolls_back(
    stage, monkeypatch, caplog, capsys,
):
    operations = []
    sessions = []
    memory_id = uuid4()

    def error_for(operation):
        if stage == operation:
            return RuntimeError(PRIVATE)
        if stage == "cancel_" + operation:
            return asyncio.CancelledError()
        return None

    class Session:
        async def __aenter__(self):
            operations.append("open")
            sessions.append(self)
            return self

        async def __aexit__(self, *args):
            operations.append("close")

        @asynccontextmanager
        async def begin(self):
            operations.append("begin")
            try:
                yield
                operations.append("commit")
                if stage == "commit":
                    raise RuntimeError(PRIVATE)
            except BaseException:
                operations.append("rollback")
                raise

    def repository(*, db):
        assert db is sessions[0]
        return Repository(operations, error=error_for("memory"))

    def store(*, db, config):
        assert db is sessions[0]
        assert config.dimension == 1024
        return Store(operations, error=error_for("vector"))

    monkeypatch.setattr(deletion_persistence, "PostgresMemoryRepository", repository)
    monkeypatch.setattr(deletion_persistence, "PgVectorStore", store)

    def run():
        return asyncio.run(deletion_persistence.delete_memory(
            memory_id=memory_id, session_factory=Session,
        ))

    if stage == "success":
        assert run() == MemoryDeletionResult(memory_id, True, 1)
    elif stage.startswith("cancel_"):
        with pytest.raises(asyncio.CancelledError):
            run()
    else:
        with pytest.raises(MemoryDeletionError) as caught:
            run()
        _assert_private_error(caught.value, caplog, capsys)
    assert len(sessions) == 1
    kinds = [operation[0] if isinstance(operation, tuple) else operation for operation in operations]
    expected = ["open", "begin", "memory"]
    if stage not in {"memory", "cancel_memory"}:
        expected.append("vector")
    if stage in {"success", "commit"}:
        expected.append("commit")
    if stage != "success":
        expected.append("rollback")
    assert kinds == expected + ["close"]


def test_invalid_identifier_does_not_open_host_session():
    def factory():
        raise AssertionError("Invalid UUID must not open storage")
    with pytest.raises(MemoryDeletionError):
        asyncio.run(deletion_persistence.delete_memory(memory_id="invalid", session_factory=factory))


def test_neutral_service_does_not_own_transaction():
    path = Path(__file__).resolve().parents[1] / "src/kulai_memory/application/deletion.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"commit", "rollback", "begin", "close"}
