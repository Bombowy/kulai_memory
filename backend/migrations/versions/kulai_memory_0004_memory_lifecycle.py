"""Version canonical content and support reversible archiving.

Revision ID: kulai_memory_0004
Revises: kulai_memory_0003
"""
from alembic import op
import sqlalchemy as sa

revision = "kulai_memory_0004"
down_revision = "kulai_memory_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")))
    op.add_column("memories", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_memories_revision_positive", "memories", "revision >= 1")
    # Prior application versions had no content editing. Annotate only the
    # exact, compatible legacy space; never bless mismatched/orphan metadata.
    op.execute(sa.text("""
        UPDATE kulai_vector_records v
        SET metadata_json = v.metadata_json || jsonb_build_object('revision', 1)
        FROM memories m
        WHERE v.namespace_key = 'kulai_memory.memories.v1'
          AND v.record_id = CAST(m.id AS text)
          AND m.revision = 1 AND m.archived_at IS NULL
          AND NOT (v.metadata_json ? 'revision')
          AND v.metadata_json @> jsonb_build_object(
              'source_memory_id', CAST(m.id AS text),
              'embedding_provider_id', 'ollama',
              'embedding_model_tag', 'bge-m3:567m-fp16',
              'embedding_dimension', 1024)
          AND jsonb_typeof(v.metadata_json->'embedding_dimension') = 'number'
          AND v.metadata_json->>'embedding_dimension' = '1024'
          AND vector_dims(v.embedding) = 1024
    """))


def downgrade() -> None:
    # Keep vector metadata: older readers tolerate additional diagnostic keys.
    op.drop_constraint("ck_memories_revision_positive", "memories", type_="check")
    op.drop_column("memories", "archived_at")
    op.drop_column("memories", "revision")
