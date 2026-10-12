"""Short read-only history snapshots, without provider calls or writes."""
from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.history import (
    MemoryDetail, MemoryHistoryCursor, MemoryHistoryItem, MemoryHistoryPage, validate_history,
)
from .application.memory import MemoryPersistenceError
from .application.ports import MemoryHistoryRepository
from .persistence import PostgresMemoryRepository


async def _read(*, session_factory, repository_factory, operation):
    try:
        async with session_factory() as session:
            try:
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                repository = (repository_factory(session) if repository_factory
                              else PostgresMemoryRepository(db=session))
                return await operation(repository)
            finally:
                await session.rollback()
    except Exception:
        raise MemoryPersistenceError() from None


async def read_memory_history(
    *, archived: bool, limit: int, session_factory: async_sessionmaker[AsyncSession],
    cursor: MemoryHistoryCursor | None = None,
    repository_factory: Callable[[AsyncSession], MemoryHistoryRepository] | None = None,
) -> MemoryHistoryPage:
    validate_history(archived=archived, limit=limit, cursor=cursor)

    async def operation(repository):
        memories = await repository.list_history(archived=archived, limit=limit, cursor=cursor)
        items = tuple(MemoryHistoryItem.from_memory(m) for m in memories[:limit])
        next_cursor = (MemoryHistoryCursor(items[-1].created_at, items[-1].id)
                       if len(memories) > limit else None)
        return MemoryHistoryPage(items, next_cursor)

    return await _read(session_factory=session_factory, repository_factory=repository_factory,
                       operation=operation)


async def read_memory_detail(
    *, memory_id: UUID, session_factory: async_sessionmaker[AsyncSession],
    repository_factory: Callable[[AsyncSession], MemoryHistoryRepository] | None = None,
) -> MemoryDetail | None:
    if not isinstance(memory_id, UUID):
        raise ValueError("Invalid Memory identifier.")

    async def operation(repository):
        memory = await repository.get_by_id(memory_id)
        return MemoryDetail.from_memory(memory) if memory is not None else None

    return await _read(session_factory=session_factory, repository_factory=repository_factory,
                       operation=operation)
