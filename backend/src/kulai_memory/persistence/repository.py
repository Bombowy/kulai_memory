"""Async PostgreSQL implementation of the MemoryRepository port."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kulai_memory.application.memory import (
    IdempotentMemoryWrite,
    Memory,
    MemoryIngestionRetiredError,
    MemoryPersistenceError,
)

from .ingestion_lock import lock_ingestion
from kulai_memory.application.deletion import MemoryDeletionRevisionConflictError
from .models import MemoryDb, MemoryIngestionTombstoneDb


def _to_domain(row: MemoryDb) -> Memory:
    return Memory(
        id=row.id,
        ingestion_id=row.ingestion_id,
        content=row.content,
        source_kind=row.source_kind,
        session_id=row.session_id,
        metadata=dict(row.metadata_json),
        created_at=row.created_at,
        revision=row.revision,
        archived_at=row.archived_at,
    )


class PostgresMemoryRepository:
    """One caller-owned AsyncSession-backed Memory repository."""

    def __init__(self, *, db: AsyncSession) -> None:
        self._db = db

    async def get_for_update(self, memory_id: UUID) -> Memory | None:
        """Same order as ingestion/delete: advisory identity -> canonical row."""
        try:
            ingestion_id = await self._db.scalar(select(MemoryDb.ingestion_id).where(MemoryDb.id == memory_id))
            if ingestion_id is None:
                return None
            await lock_ingestion(self._db, ingestion_id)
            row = await self._db.scalar(select(MemoryDb).where(MemoryDb.id == memory_id).with_for_update())
            if row is None:
                return None
            if row.ingestion_id != ingestion_id:
                raise MemoryPersistenceError()
            return _to_domain(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise MemoryPersistenceError() from None

    async def save_lifecycle(self, memory: Memory) -> Memory:
        """Caller must hold get_for_update's lock; change only lifecycle fields."""
        try:
            row = await self._db.scalar(update(MemoryDb).where(MemoryDb.id == memory.id).values(
                content=memory.content, revision=memory.revision, archived_at=memory.archived_at,
            ).returning(MemoryDb).execution_options(populate_existing=True))
            if row is None:
                raise MemoryPersistenceError()
            return _to_domain(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise MemoryPersistenceError() from None

    async def _require_active_ingestion(self, ingestion_id: UUID) -> None:
        await lock_ingestion(self._db, ingestion_id)
        result = await self._db.execute(
            select(MemoryIngestionTombstoneDb.ingestion_id).where(
                MemoryIngestionTombstoneDb.ingestion_id == ingestion_id,
            ),
        )
        if result.scalar_one_or_none() is not None:
            raise MemoryIngestionRetiredError() from None

    async def create(self, memory: Memory) -> Memory:
        row = MemoryDb(
            id=memory.id,
            ingestion_id=memory.ingestion_id,
            content=memory.content,
            revision=memory.revision,
            archived_at=memory.archived_at,
            source_kind=memory.source_kind.value,
            session_id=memory.session_id,
            metadata_json=dict(memory.metadata),
            created_at=memory.created_at,
        )
        try:
            await self._require_active_ingestion(memory.ingestion_id)
            self._db.add(row)
            await self._db.flush()
            return _to_domain(row)
        except asyncio.CancelledError:
            raise
        except MemoryIngestionRetiredError:
            raise
        except Exception as exc:
            raise MemoryPersistenceError from exc

    async def create_or_get_by_ingestion_id(
        self, memory: Memory
    ) -> IdempotentMemoryWrite:
        """Atomically insert or return the row owning ``memory.ingestion_id``."""

        statement = (
            insert(MemoryDb)
            .values(
                id=memory.id,
                ingestion_id=memory.ingestion_id,
                content=memory.content,
                revision=memory.revision,
                archived_at=memory.archived_at,
                source_kind=memory.source_kind.value,
                session_id=memory.session_id,
                metadata_json=dict(memory.metadata),
                created_at=memory.created_at,
            )
            .on_conflict_do_nothing(index_elements=[MemoryDb.ingestion_id])
            .returning(MemoryDb)
        )
        try:
            await self._require_active_ingestion(memory.ingestion_id)
            result = await self._db.execute(statement)
            inserted = result.scalar_one_or_none()
            if inserted is not None:
                return IdempotentMemoryWrite(memory=_to_domain(inserted), created=True)

            existing_result = await self._db.execute(
                select(MemoryDb).where(MemoryDb.ingestion_id == memory.ingestion_id)
            )
            existing = existing_result.scalar_one_or_none()
            if existing is None:
                raise MemoryPersistenceError()
            return IdempotentMemoryWrite(memory=_to_domain(existing), created=False)
        except asyncio.CancelledError:
            raise
        except (MemoryPersistenceError, MemoryIngestionRetiredError):
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

    async def delete_by_id(self, memory_id: UUID, *, expected_revision: int | None = None) -> bool:
        """Retire identity and delete the row in the caller-owned transaction.

        Discover without a row lock, then lock ingestion before rechecking the
        canonical row FOR UPDATE. This is the same order used by ingestion.
        """

        try:
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
                raise MemoryPersistenceError()
            identity = await self._db.execute(
                select(MemoryDb.ingestion_id).where(MemoryDb.id == memory_id),
            )
            ingestion_id = identity.scalar_one_or_none()
            if ingestion_id is None:
                retired = await self._db.execute(
                    select(MemoryIngestionTombstoneDb.ingestion_id).where(
                        MemoryIngestionTombstoneDb.memory_id == memory_id,
                    ),
                )
                ingestion_id = retired.scalar_one_or_none()
            if ingestion_id is None:
                if expected_revision is not None:
                    raise MemoryDeletionRevisionConflictError()
                return False

            await lock_ingestion(self._db, ingestion_id)
            canonical = await self._db.execute(
                select(MemoryDb.ingestion_id, MemoryDb.revision)
                .where(MemoryDb.id == memory_id)
                .with_for_update(),
            )
            current = canonical.one_or_none()
            if current is None:
                if expected_revision is not None:
                    raise MemoryDeletionRevisionConflictError()
                return False
            current_ingestion_id, revision = current
            if current_ingestion_id != ingestion_id:
                raise MemoryPersistenceError()
            if expected_revision is not None and revision != expected_revision:
                raise MemoryDeletionRevisionConflictError()

            tombstone = await self._db.execute(
                insert(MemoryIngestionTombstoneDb)
                .values(ingestion_id=ingestion_id, memory_id=memory_id)
                .on_conflict_do_nothing(
                    index_elements=[MemoryIngestionTombstoneDb.ingestion_id],
                )
                .returning(MemoryIngestionTombstoneDb.memory_id),
            )
            if tombstone.scalar_one_or_none() is None:
                existing = await self._db.execute(
                    select(MemoryIngestionTombstoneDb.memory_id).where(
                        MemoryIngestionTombstoneDb.ingestion_id == ingestion_id,
                    ),
                )
                if existing.scalar_one_or_none() != memory_id:
                    raise MemoryPersistenceError()

            statement = (
                delete(MemoryDb).where(MemoryDb.id == memory_id).returning(MemoryDb.id)
            )
            result = await self._db.execute(statement)
            return result.scalar_one_or_none() is not None
        except asyncio.CancelledError:
            raise
        except (MemoryPersistenceError, MemoryDeletionRevisionConflictError):
            raise
        except Exception as exc:
            raise MemoryPersistenceError from exc

    async def list_library(self, *, archived: bool, limit: int) -> tuple[Memory, ...]:
        if type(archived) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid Memory library filter or limit.")
        statement = select(MemoryDb).where(
            MemoryDb.archived_at.is_not(None) if archived else MemoryDb.archived_at.is_(None)
        ).order_by(MemoryDb.created_at.desc(), MemoryDb.id.desc()).limit(limit)
        try:
            result = await self._db.execute(statement)
            return tuple(_to_domain(row) for row in result.scalars().all())
        except asyncio.CancelledError:
            raise
        except Exception:
            raise MemoryPersistenceError() from None

    async def list_recent(self, *, limit: int, include_archived: bool = False) -> tuple[Memory, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Memory list limit must be between 1 and 100.")
        statement = (
            select(MemoryDb)
            .order_by(MemoryDb.created_at.desc(), MemoryDb.id.desc())
            .limit(limit)
        )
        if not include_archived:
            statement = statement.where(MemoryDb.archived_at.is_(None))
        try:
            result = await self._db.execute(statement)
            return tuple(_to_domain(row) for row in result.scalars().all())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MemoryPersistenceError from exc
