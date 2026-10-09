"""Short read-only canonical Library reads; no model/provider work."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application.memory import Memory, MemoryPersistenceError
from .application.ports import MemoryLibraryRepository
from .persistence import PostgresMemoryRepository


async def read_memory_library(
    *, archived: bool, limit: int, session_factory: async_sessionmaker[AsyncSession],
    repository_factory: Callable[[AsyncSession], MemoryLibraryRepository] | None = None,
) -> tuple[Memory, ...]:
    if type(archived) is not bool or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid Memory library filter or limit.")
    try:
        async with session_factory() as session:
            try:
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                repository = repository_factory(session) if repository_factory else PostgresMemoryRepository(db=session)
                return await repository.list_library(archived=archived, limit=limit)
            finally:
                await session.rollback()
    except Exception:
        raise MemoryPersistenceError() from None
