"""Initial dimension-configured PostgreSQL pgvector schema."""

from collections.abc import Sequence

from alembic import context, op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "kvectorstorepg_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("kulai_vector_store_pgvector",)
depends_on: str | Sequence[str] | None = None

DIMENSION_ARGUMENT = "kulai_vector_dimension"
TABLE_NAME = "kulai_vector_records"


def _configured_dimension() -> int:
    raw = context.get_x_argument(as_dictionary=True).get(DIMENSION_ARGUMENT)
    try:
        dimension = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        dimension = 0
    if dimension < 1 or raw is None or str(dimension) != str(raw).strip():
        raise RuntimeError(
            "Alembic requires -x kulai_vector_dimension=<positive integer>."
        )
    return dimension


def upgrade() -> None:
    dimension = _configured_dimension()
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.create_table(
        TABLE_NAME,
        sa.Column("pk", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(length=512),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("record_id", sa.String(length=512), nullable=False),
        sa.Column("embedding", VECTOR(dimension), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("pk"),
        sa.UniqueConstraint(
            "namespace_key",
            "record_id",
            name="uq_kulai_vector_records_namespace_record",
        ),
    )
    op.create_index(
        "ix_kulai_vector_records_namespace",
        TABLE_NAME,
        ["namespace_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_kulai_vector_records_namespace", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)

