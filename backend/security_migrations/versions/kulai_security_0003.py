"""PL: trwałe sesje i historia replay. EN: independent, additive security migration."""
from alembic import op
import sqlalchemy as sa
from kulai_memory.security_persistence.schema_v3 import (
    SESSION_TABLE, REFRESH_TABLE, session_definition, refresh_definition,
)

revision = 'kulai_security_0003'
down_revision = 'kulai_security_0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(SESSION_TABLE, *session_definition(), schema='public')
    op.create_table(REFRESH_TABLE, *refresh_definition(), schema='public')
    op.create_index('ix_security_refreshes_session', REFRESH_TABLE, ['session_id'], schema='public')
    op.create_index('ux_security_refreshes_active', REFRESH_TABLE, ['session_id'], unique=True,
                    schema='public', postgresql_where=sa.text('consumed_at IS NULL'))


def downgrade():
    raise RuntimeError('Destructive security downgrade is not supported.')
