from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import traceback
from uuid import uuid4

import pytest

from backend.tests.test_memory_deletion import Store
from kulai_memory.application.lifecycle import (
    MemoryLifecycleError, MemoryLifecycleService, MemoryLifecycleStatus,
    MemoryNotFoundError, MemoryRevisionConflictError,
)
from kulai_memory.application.memory import Memory, MemoryArchivedError, MemoryService, IdempotentMemoryWrite
from kulai_memory.application.indexing import MemoryIndexingArchivedError, MemoryIndexingStaleRevisionError, MemoryIndexingService
from kulai_memory import indexing_persistence
from backend.tests.test_automatic_indexing import Provider, Sessions, metadata

PRIVATE = "PRIVATE_LIFECYCLE_CONTENT_VECTOR_PASSWORD_SENTINEL"


class Repository:
    def __init__(self, memory):
        self.memory = memory
        self.saved = []

    async def get_for_update(self, memory_id):
        return self.memory if self.memory and self.memory.id == memory_id else None

    async def save_lifecycle(self, memory):
        self.saved.append(memory)
        self.memory = memory
        return memory

    async def create_or_get_by_ingestion_id(self, candidate):
        return IdempotentMemoryWrite(self.memory, False)


def service(memory):
    repo, events = Repository(memory), []
    return MemoryLifecycleService(repository=repo, store=Store(events)), repo, events


def test_edit_preserves_identity_and_updates_revision_with_exact_content():
    original = Memory(content=PRIVATE, metadata={"diagnostic": "unchanged"}, session_id=uuid4())
    core, repo, events = service(original)
    changed = asyncio.run(core.edit(memory_id=original.id, content="  exact edited content\n", expected_revision=1))
    assert changed.status is MemoryLifecycleStatus.EDITED
    assert changed.memory.revision == 2 and changed.memory.content == "  exact edited content\n"
    assert changed.memory.model_dump(exclude={"content", "revision"}) == original.model_dump(exclude={"content", "revision"})
    assert len(events) == len(repo.saved) == 1
    assert events[0][1].ids == (str(original.id),)
    assert events[0][1].namespace == "kulai_memory.memories.v1"


def test_same_content_is_noop():
    memory = Memory(content=PRIVATE)
    core, repo, events = service(memory)
    result = asyncio.run(core.edit(memory_id=memory.id, content=PRIVATE, expected_revision=1))
    assert result.status is MemoryLifecycleStatus.UNCHANGED and result.memory == memory
    assert not events and not repo.saved


@pytest.mark.parametrize("revision", [0, True, "1", 2])
def test_invalid_or_stale_expected_revision_has_no_changes(revision):
    memory = Memory(content=PRIVATE)
    core, repo, events = service(memory)
    with pytest.raises(MemoryRevisionConflictError if revision == 2 else MemoryLifecycleError):
        asyncio.run(core.edit(memory_id=memory.id, content="new", expected_revision=revision))
    assert not repo.saved and not events


@pytest.mark.parametrize("content", ["", " \n ", None, 2])
def test_invalid_content_rejected(content):
    memory = Memory(content=PRIVATE)
    core, repo, events = service(memory)
    with pytest.raises(MemoryLifecycleError):
        asyncio.run(core.edit(memory_id=memory.id, content=content, expected_revision=1))
    assert not repo.saved and not events


def test_archive_restore_preserve_revision_timestamp_and_no_tombstones():
    async def scenario():
        memory = Memory(content=PRIVATE, revision=2)
        core, repo, events = service(memory)
        archived = await core.archive(memory_id=memory.id)
        assert archived.memory.revision == 2 and archived.memory.archived_at.tzinfo == UTC
        again = await core.archive(memory_id=memory.id)
        assert again.status is MemoryLifecycleStatus.ALREADY_ARCHIVED
        assert again.memory.archived_at == archived.memory.archived_at and len(repo.saved) == 1
        with pytest.raises(MemoryArchivedError):
            await core.edit(memory_id=memory.id, content="new", expected_revision=2)
        restored = await core.restore(memory_id=memory.id)
        assert restored.status is MemoryLifecycleStatus.RESTORED and restored.memory == memory
        assert (await core.restore(memory_id=memory.id)).status is MemoryLifecycleStatus.ALREADY_ACTIVE
        assert len(events) == 2 and len(repo.saved) == 2
    asyncio.run(scenario())


def test_archived_ingestion_is_terminal_and_not_restored():
    memory = Memory(content=PRIVATE, archived_at=datetime.now(UTC))
    repo = Repository(memory)
    with pytest.raises(MemoryArchivedError) as caught:
        asyncio.run(MemoryService(repository=repo).create_memory_idempotent(ingestion_id=memory.ingestion_id, content=PRIVATE))
    assert caught.value.code == "memory.archived" and repo.memory == memory
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("action", ["archive", "restore", "edit"])
def test_missing_canonical_is_not_recreated(action):
    core, repo, events = service(None)
    kwargs = dict(memory_id=uuid4())
    if action == "edit": kwargs.update(content="new", expected_revision=1)
    with pytest.raises(MemoryNotFoundError):
        asyncio.run(getattr(core, action)(**kwargs))
    assert not repo.saved and not events


@pytest.mark.parametrize("error", [RuntimeError(PRIVATE), asyncio.CancelledError()])
def test_vector_failure_public_error_safe_and_cancellation_propagates(error):
    memory = Memory(content=PRIVATE)
    repo = Repository(memory)
    core = MemoryLifecycleService(repository=repo, store=Store([], error=error))
    with pytest.raises(asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else MemoryLifecycleError) as caught:
        asyncio.run(core.archive(memory_id=memory.id))
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("mutation", ["edit", "archive"])
def test_stale_detached_memory_stops_before_embedding(monkeypatch, mutation):
    async def scenario():
        memory = Memory(content=PRIVATE)
        current = memory.model_copy(update={"revision": 2} if mutation == "edit" else {"archived_at": datetime.now(UTC)})
        sessions, provider = Sessions(current), Provider()
        expected = MemoryIndexingStaleRevisionError if mutation == "edit" else MemoryIndexingArchivedError
        with pytest.raises(expected):
            await indexing_persistence.ensure_memory_indexed(memory=memory,
                service=MemoryIndexingService(provider=provider, expected_dimension=1024), session_factory=sessions)
        assert not provider.requests and not sessions.writes
    asyncio.run(scenario())


def test_archived_prepare_never_embeds():
    provider = Provider()
    with pytest.raises(MemoryIndexingArchivedError):
        asyncio.run(MemoryIndexingService(provider=provider, expected_dimension=1024).prepare(
            memory=Memory(content=PRIVATE, archived_at=datetime.now(UTC))))
    assert not provider.requests
