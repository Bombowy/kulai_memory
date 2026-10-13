"""Create independent security identities/credentials; never touch canonical tables."""
from alembic import op
from kulai_memory.security_persistence.schema_v1 import (
    DEVICE_TABLE, TOKEN_TABLE, device_definition, token_definition,
)

revision = "kulai_security_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(DEVICE_TABLE, *device_definition(), schema="public")
    op.create_table(TOKEN_TABLE, *token_definition(), schema="public")
    op.create_index("ix_security_tokens_device", TOKEN_TABLE, ["device_id"], schema="public")


def downgrade() -> None:
    # PL/EN: no automatic destructive downgrade of durable trust/revocation state.
    raise RuntimeError("Security downgrade requires a separately reviewed recovery procedure.")
