"""Immutable additive session schema; v1/v2 and canonical tables remain unchanged."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from .schema_v1 import metadata, DEVICE_TABLE, TOKEN_TABLE
from .schema_v2 import PAIRING_TABLE

SECURITY_HEAD = 'kulai_security_0003'
SESSION_TABLE = 'kulai_security_sessions'
REFRESH_TABLE = 'kulai_security_refreshes'


def session_definition() -> tuple:
    return (
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('device_id', UUID(as_uuid=True), sa.ForeignKey(f'public.{DEVICE_TABLE}.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('pairing_id', UUID(as_uuid=True), sa.ForeignKey(f'public.{PAIRING_TABLE}.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('absolute_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('actions', ARRAY(sa.Text()), nullable=False),
        sa.Column('access_ttl_seconds', sa.Integer(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.CheckConstraint("absolute_expires_at > created_at AND absolute_expires_at <= created_at + interval '90 days'", name='ck_session_lifetime'),
        sa.CheckConstraint('access_ttl_seconds >= 1 AND access_ttl_seconds <= 3600 AND generation >= 0 AND generation <= 4096', name='ck_session_limits'),
        sa.CheckConstraint("cardinality(actions) >= 1 AND cardinality(actions) <= 4 AND array_position(actions, NULL) IS NULL AND actions <@ ARRAY['memory.read', 'memory.write', 'assistant.ask', 'assistant.speech_plan']::text[]", name='ck_session_actions'),
    )


def refresh_definition() -> tuple:
    return (
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('session_id', UUID(as_uuid=True), sa.ForeignKey(f'public.{SESSION_TABLE}.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('access_id', UUID(as_uuid=True), sa.ForeignKey(f'public.{TOKEN_TABLE}.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('digest', sa.Text(), nullable=False, unique=True),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name='ck_refresh_digest'),
        sa.CheckConstraint("expires_at > issued_at AND expires_at <= issued_at + interval '30 days'", name='ck_refresh_lifetime'),
        sa.CheckConstraint('consumed_at IS NULL OR consumed_at >= issued_at', name='ck_refresh_consumption'),
    )


sessions = sa.Table(SESSION_TABLE, metadata, *session_definition())
refreshes = sa.Table(REFRESH_TABLE, metadata, *refresh_definition())
