"""Async PostgreSQL implementation of the MemoryRepository port."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kulai_memory.application.memory import Memory, MemoryPersistenceError

from .models import MemoryDb


def _to_domain(row: MemoryDb) -> Memory:
    return Memory(
        id=row.id,
        content=row.content,
        source_kind=row.source_kind,
        session_id=row.session_id,
        metadata=dict(row.metadata_json),
        created_at=row.created_at,
    )


class PostgresMemoryRepository:
    """One caller-owned AsyncSession-backed Memory repository."""

    def __init__(self, *, db: AsyncSession) -> None:
        self._db = db

    async def create(self, memory: Memory) -> Memory:
        row = MemoryDb(
            id=memory.id,
            content=memory.content,
            source_kind=memory.source_kind.value,
            session_id=memory.session_id,
            metadata_json=dict(memory.metadata),
            created_at=memory.created_at,
        )
        try:
            self._db.add(row)
            await self._db.flush()
            return _to_domain(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MemoryPersistenceError from exc

    async def get_by_id(self, memory_id: UUID) -> Memory | None:
        statement = select(MemoryDb).where(MemoryDb.id == memory_id)
        try:
            result = await self._db.execute(statement)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MemoryPersistenceError from exc

    async def list_recent(self, *, limit: int) -> tuple[Memory, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Memory list limit must be between 1 and 100.")
        statement = (
            select(MemoryDb)
            .order_by(MemoryDb.created_at.desc(), MemoryDb.id.desc())
            .limit(limit)
        )
        try:
            result = await self._db.execute(statement)
            return tuple(_to_domain(row) for row in result.scalars().all())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MemoryPersistenceError from exc
