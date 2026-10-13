"""Additive pairing schema. PL/EN: canonical tables are never altered."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from .schema_v1 import metadata, DEVICE_TABLE, TOKEN_TABLE

SECURITY_HEAD = 'kulai_security_0002'
PAIRING_TABLE = 'kulai_security_pairings'
RATE_TABLE = 'kulai_security_pairing_rate'


def pairing_definition() -> tuple:
    return (
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('invite_digest', sa.Text(), nullable=False, unique=True),
        sa.Column('receiver_digest', sa.Text(), nullable=True),
        sa.Column('client_id', UUID(as_uuid=True), nullable=True),
        sa.Column('device_id', UUID(as_uuid=True), sa.ForeignKey(f'public.{DEVICE_TABLE}.id', ondelete='RESTRICT'), nullable=True, unique=True),
        sa.Column('device_label', sa.Text(), nullable=True),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('token_id', UUID(as_uuid=True), sa.ForeignKey(f'public.{TOKEN_TABLE}.id', ondelete='RESTRICT'), nullable=True, unique=True),
        sa.Column('actions', ARRAY(sa.Text()), nullable=False),
        sa.Column('token_ttl_seconds', sa.Integer(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.CheckConstraint("invite_digest ~ '^[0-9a-f]{64}$' AND (receiver_digest IS NULL OR receiver_digest ~ '^[0-9a-f]{64}$')", name='ck_pairing_digests'),
        sa.CheckConstraint("state IN ('invited','pending','approved','claimed','denied','cancelled')", name='ck_pairing_state'),
        sa.CheckConstraint("expires_at > created_at AND expires_at <= created_at + interval '5 minutes'", name='ck_pairing_lifetime'),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 8 AND token_ttl_seconds >= 1 AND token_ttl_seconds <= 86400", name='ck_pairing_limits'),
        sa.CheckConstraint("cardinality(actions) >= 1 AND cardinality(actions) <= 4 AND array_position(actions, NULL) IS NULL AND actions <@ ARRAY['memory.read', 'memory.write', 'assistant.ask', 'assistant.speech_plan']::text[]", name='ck_pairing_actions'),
        sa.CheckConstraint("(state = 'invited' AND receiver_digest IS NULL AND device_id IS NULL AND client_id IS NULL AND device_label IS NULL) OR (state <> 'invited' AND receiver_digest IS NOT NULL AND device_id IS NOT NULL AND client_id IS NOT NULL AND device_label IS NOT NULL)", name='ck_pairing_binding'),
        sa.CheckConstraint("(state IN ('approved','claimed') AND approved_at IS NOT NULL) OR (state NOT IN ('approved','claimed') AND approved_at IS NULL)", name='ck_pairing_approval'),
        sa.CheckConstraint("(state = 'claimed' AND token_id IS NOT NULL) OR (state <> 'claimed' AND token_id IS NULL)", name='ck_pairing_claim'),
        sa.CheckConstraint("device_label IS NULL OR device_label ~ '^[A-Za-z0-9 ._-]{1,48}$'", name='ck_pairing_label'),
    )


def rate_definition() -> tuple:
    return (
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.CheckConstraint('id = 1 AND attempts >= 0 AND attempts <= 120', name='ck_pairing_rate'),
    )


pairings = sa.Table(PAIRING_TABLE, metadata, *pairing_definition())
rate = sa.Table(RATE_TABLE, metadata, *rate_definition())
