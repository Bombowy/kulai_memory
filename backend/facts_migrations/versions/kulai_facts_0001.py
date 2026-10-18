"""Independent, explicitly installed marker-owned fact storage."""

from alembic import op
from kulai_memory.fact_persistence.schema_v1 import DDL

revision = "kulai_facts_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Fact data removal requires an explicit reviewed privacy workflow.")
