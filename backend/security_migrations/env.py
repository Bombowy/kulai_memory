"""Separate security graph. Accept only an explicitly supplied guarded connection."""
from alembic import context
from kulai_memory.security_persistence.schema_v1 import VERSION_TABLE, metadata

connection = context.config.attributes.get("connection")
if context.is_offline_mode() or connection is None:
    raise RuntimeError("Use the guarded security migration workflow.")
context.configure(connection=connection, target_metadata=metadata, version_table=VERSION_TABLE, version_table_schema="public")
with context.begin_transaction():
    context.run_migrations()
