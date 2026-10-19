"""Durable source epochs and canonical transactional invalidation, owned-only."""

from alembic import op
from kulai_memory.fact_persistence.schema_v2 import DDL, CONVERT_LEGACY, INVALIDATE_LEGACY

revision = "kulai_facts_0002"
down_revision = "kulai_facts_0001"
branch_labels = None
depends_on = None


def upgrade():
    # Exclusive maintenance on this owned test DB: no partially backfilled epoch
    # can race a canonical mutation. Canonical graph/schema columns stay intact.
    op.execute("LOCK TABLE public.memories IN ACCESS EXCLUSIVE MODE")
    for statement in DDL:
        op.execute(statement)
    op.execute("ALTER TABLE public.kulai_fact_source_versions DISABLE TRIGGER immutable_fact_source_version")
    op.execute(CONVERT_LEGACY)
    op.execute("ALTER TABLE public.kulai_fact_source_versions ENABLE TRIGGER immutable_fact_source_version")
    op.execute(INVALIDATE_LEGACY)


def downgrade():
    raise RuntimeError("Durable source generations cannot be downgraded safely.")
