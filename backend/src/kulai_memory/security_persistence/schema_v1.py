"""Immutable v1 schema shared by the separate Alembic revision and SQL adapter.

PL: przyszłe wersje wymagają nowej migracji, bez zmiany tego kontraktu.
EN: never create tables on application startup; only the reviewed Alembic graph.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

SECURITY_REVISION = "kulai_security_0001"
VERSION_TABLE = "kulai_security_version"
DEVICE_TABLE = "kulai_security_devices"
TOKEN_TABLE = "kulai_security_tokens"


def device_definition() -> tuple:
    return (
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'trusted', 'revoked')", name="ck_security_device_status"),
    )


def token_definition() -> tuple:
    return (
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey(f"public.{DEVICE_TABLE}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actions", ARRAY(sa.Text()), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("digest", name="uq_security_token_digest"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_security_token_digest"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_security_token_lifetime"),
        sa.CheckConstraint("cardinality(actions) >= 1 AND cardinality(actions) <= 4 AND array_position(actions, NULL) IS NULL "
            "AND actions <@ ARRAY['memory.read', 'memory.write', 'assistant.ask', 'assistant.speech_plan']::text[]",
            name="ck_security_token_actions"),
    )


metadata = sa.MetaData(schema="public")
devices = sa.Table(DEVICE_TABLE, metadata, *device_definition())
tokens = sa.Table(TOKEN_TABLE, metadata, *token_definition())
