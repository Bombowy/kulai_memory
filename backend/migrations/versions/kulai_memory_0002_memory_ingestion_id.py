"""Add the idempotent Memory ingestion identifier.

Revision ID: kulai_memory_0002
Revises: kulai_memory_0001
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "kulai_memory_0002"
down_revision: str | Sequence[str] | None = "kulai_memory_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column(
            "ingestion_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.execute("UPDATE memories SET ingestion_id = id WHERE ingestion_id IS NULL")
    op.alter_column("memories", "ingestion_id", nullable=False)
    op.create_unique_constraint(
        "uq_memories_ingestion_id",
        "memories",
        ["ingestion_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_memories_ingestion_id",
        "memories",
        type_="unique",
    )
    op.drop_column("memories", "ingestion_id")
