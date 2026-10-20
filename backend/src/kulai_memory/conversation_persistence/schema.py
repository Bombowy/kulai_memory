"""V2.1 adapter metadata matching the explicit 0005 migration.

No shared kulai_db.Base registration or startup DDL. Historical Alembic
definitions are frozen independently of mutable application limits here.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from kulai_memory.conversations.domain import MAX_MEMORY_REFERENCES, MAX_MESSAGE_CHARS, MAX_TITLE_CHARS

metadata = sa.MetaData(schema="public")

conversations = sa.Table(
    "kulai_conversations", metadata,
    sa.Column("conversation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("title", sa.String(MAX_TITLE_CHARS), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("last_turn", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.CheckConstraint("last_turn >= 0 AND last_turn <= 4611686018427387903", name="ck_conversation_last_turn"),
    sa.CheckConstraint("last_activity_at >= created_at", name="ck_conversation_activity"),
    sa.CheckConstraint("title IS NULL OR title ~ '[^[:space:]]'", name="ck_conversation_title"),
)
sa.Index("ix_conversations_activity_id", conversations.c.last_activity_at.desc(), conversations.c.conversation_id.desc())

turns = sa.Table(
    "kulai_conversation_turns", metadata,
    sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey(conversations.c.conversation_id, ondelete="CASCADE"), nullable=False),
    sa.Column("turn_number", sa.BigInteger(), nullable=False),
    sa.UniqueConstraint("conversation_id", "turn_number", name="uq_conversation_turn_number"),
    sa.UniqueConstraint("request_id", "conversation_id", "turn_number", name="uq_conversation_turn_identity"),
    sa.CheckConstraint("turn_number >= 1 AND turn_number <= 4611686018427387903", name="ck_conversation_turn_number"),
)

messages = sa.Table(
    "kulai_conversation_messages", metadata,
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
    sa.ForeignKeyConstraint(
        ["request_id", "conversation_id", "turn_number"],
        [turns.c.request_id, turns.c.conversation_id, turns.c.turn_number],
        ondelete="CASCADE", name="fk_conversation_message_turn",
    ),
    sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_message_sequence"),
    sa.UniqueConstraint("request_id", "role", name="uq_conversation_message_role"),
    sa.CheckConstraint(f"char_length(content) BETWEEN 1 AND {MAX_MESSAGE_CHARS} AND content ~ '[^[:space:]]'",
                       name="ck_conversation_message_content"),
    sa.CheckConstraint(
        "(role = 'user' AND input_kind IN ('text', 'voice') AND input_kind IS NOT NULL "
        "AND sequence = 2 * turn_number - 1 AND cardinality(memory_ids) = 0) OR "
        "(role = 'assistant' AND input_kind IS NULL AND sequence = 2 * turn_number)",
        name="ck_conversation_message_role_sequence",
    ),
    sa.CheckConstraint(
        f"cardinality(memory_ids) <= {MAX_MEMORY_REFERENCES} AND array_position(memory_ids, NULL) IS NULL "
        "AND (cardinality(memory_ids) = 0 OR array_ndims(memory_ids) = 1)",
        name="ck_conversation_message_references",
    ),
)
