"""Persistent one-use pairing and cross-process abuse limits."""
from alembic import op
import sqlalchemy as sa
from kulai_memory.security_persistence.schema_v2 import PAIRING_TABLE, RATE_TABLE, pairing_definition, rate_definition

revision = 'kulai_security_0002'
down_revision = 'kulai_security_0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(PAIRING_TABLE, *pairing_definition(), schema='public')
    op.create_table(RATE_TABLE, *rate_definition(), schema='public')
    op.create_index('ix_security_pairings_expiry', PAIRING_TABLE, ['expires_at'], schema='public')


def downgrade():
    raise RuntimeError('Destructive security downgrade is not supported.')
