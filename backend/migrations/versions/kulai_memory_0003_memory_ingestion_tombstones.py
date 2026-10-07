"""Retire hard-deleted Memory ingestion identities durably.

Revision ID: kulai_memory_0003
Revises: kulai_memory_0002
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "kulai_memory_0003"
down_revision: str | Sequence[str] | None = "kulai_memory_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_ingestion_tombstones",
        sa.Column("ingestion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("ingestion_id", name="pk_memory_ingestion_tombstones"),
        sa.UniqueConstraint("memory_id", name="uq_memory_ingestion_tombstones_memory_id"),
    )


def downgrade() -> None:
    op.drop_table("memory_ingestion_tombstones")
