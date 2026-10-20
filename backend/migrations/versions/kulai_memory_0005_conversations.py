"""Persist complete conversation turns independently of canonical Memories.

Revision ID: kulai_memory_0005
Revises: kulai_memory_0004

Frozen V2.1 definitions: do not import mutable application models or limits.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "kulai_memory_0005"
down_revision = "kulai_memory_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kulai_conversations",
        sa.Column("conversation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_turn", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("last_turn >= 0 AND last_turn <= 4611686018427387903", name="ck_conversation_last_turn"),
        sa.CheckConstraint("last_activity_at >= created_at", name="ck_conversation_activity"),
        sa.CheckConstraint("title IS NULL OR title ~ '[^[:space:]]'", name="ck_conversation_title"),
        schema="public",
    )
    op.create_index("ix_conversations_activity_id", "kulai_conversations",
                    [sa.text("last_activity_at DESC"), sa.text("conversation_id DESC")], schema="public")
    op.create_table(
        "kulai_conversation_turns",
        sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("public.kulai_conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_number", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("conversation_id", "turn_number", name="uq_conversation_turn_number"),
        sa.UniqueConstraint("request_id", "conversation_id", "turn_number", name="uq_conversation_turn_identity"),
        sa.CheckConstraint("turn_number >= 1 AND turn_number <= 4611686018427387903", name="ck_conversation_turn_number"),
        schema="public",
    )
    op.create_table(
        "kulai_conversation_messages",
        sa.Column("message_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("turn_number", sa.BigInteger(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_kind", sa.Text(), nullable=True),
        sa.Column("memory_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")),
        sa.ForeignKeyConstraint(["request_id", "conversation_id", "turn_number"],
            ["public.kulai_conversation_turns.request_id", "public.kulai_conversation_turns.conversation_id", "public.kulai_conversation_turns.turn_number"],
            ondelete="CASCADE", name="fk_conversation_message_turn"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_message_sequence"),
        sa.UniqueConstraint("request_id", "role", name="uq_conversation_message_role"),
        sa.CheckConstraint("char_length(content) BETWEEN 1 AND 16000 AND content ~ '[^[:space:]]'", name="ck_conversation_message_content"),
        sa.CheckConstraint(
            "(role = 'user' AND input_kind IN ('text', 'voice') AND input_kind IS NOT NULL "
            "AND sequence = 2 * turn_number - 1 AND cardinality(memory_ids) = 0) OR "
            "(role = 'assistant' AND input_kind IS NULL AND sequence = 2 * turn_number)",
            name="ck_conversation_message_role_sequence"),
        sa.CheckConstraint(
            "cardinality(memory_ids) <= 20 AND array_position(memory_ids, NULL) IS NULL "
            "AND (cardinality(memory_ids) = 0 OR array_ndims(memory_ids) = 1)",
            name="ck_conversation_message_references"),
        schema="public",
    )


def downgrade() -> None:
    op.drop_table("kulai_conversation_messages", schema="public")
    op.drop_table("kulai_conversation_turns", schema="public")
    op.drop_table("kulai_conversations", schema="public")
