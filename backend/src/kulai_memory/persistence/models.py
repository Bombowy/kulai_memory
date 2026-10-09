"""SQLAlchemy models for host-owned Memory persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from kulai_db import Base
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class MemoryDb(Base):
    """Private PostgreSQL representation of a domain Memory."""

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_created_at", "created_at"),
        UniqueConstraint("ingestion_id", name="uq_memories_ingestion_id"),
        CheckConstraint("revision >= 1", name="ck_memories_revision_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    ingestion_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=uuid4,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class MemoryIngestionTombstoneDb(Base):
    """Technical identities only; deliberately no relationship to deleted rows."""

    __tablename__ = "memory_ingestion_tombstones"
    __table_args__ = (
        PrimaryKeyConstraint("ingestion_id", name="pk_memory_ingestion_tombstones"),
        UniqueConstraint("memory_id", name="uq_memory_ingestion_tombstones_memory_id"),
    )

    ingestion_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    memory_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


def register_memory_orm_models() -> type[MemoryDb]:
    """Expose host models already declared on shared Base metadata."""

    return MemoryDb
