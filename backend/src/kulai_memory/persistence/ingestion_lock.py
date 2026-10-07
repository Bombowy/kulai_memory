"""Cross-process serialization of creation and retirement of ingestion UUIDs.

Ingestion/deletion acquire this transaction lock before canonical row locks.
Indexing acquires only the canonical row lock, then writes its vector; it never
acquires an ingestion lock. All locks are released by caller commit/rollback.
Use PostgreSQL READ COMMITTED so checks after a waiting lock see committed state.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import BigInteger, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession


_LOCK_DOMAIN = b"kulai_memory.ingestion.v1\0"
_LOCK_STATEMENT = text("SELECT pg_advisory_xact_lock(:ingestion_key)").bindparams(
    bindparam("ingestion_key", type_=BigInteger()),
)


def ingestion_lock_key(ingestion_id: UUID) -> int:
    """Stable signed int64; a hash collision only serializes unrelated UUIDs."""

    digest = hashlib.sha256(_LOCK_DOMAIN + ingestion_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_ingestion(db: AsyncSession, ingestion_id: UUID) -> None:
    await db.execute(
        _LOCK_STATEMENT, {"ingestion_key": ingestion_lock_key(ingestion_id)},
    )
