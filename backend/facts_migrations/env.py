"""Fact DDL accepts only a live, verified marker-owned connection. Never main."""

from alembic import context
from sqlalchemy import text

from kulai_memory.database_safety import OwnedTemporaryDatabase, validate_owned_database_name
from kulai_memory.fact_persistence.schema_v1 import VERSION_TABLE

config = context.config
connection = config.attributes.get("connection")
owned = config.attributes.get("owned")
if context.is_offline_mode() or connection is None or not isinstance(owned, OwnedTemporaryDatabase):
    raise RuntimeError("Fact migrations require the marker-owned test workflow.")
validate_owned_database_name(owned.name, source_database=config.attributes["source_database"], kind=owned.kind)
actual, marker = connection.execute(text(
    "SELECT current_database(),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"
)).one()
if actual != owned.name or marker != owned.marker:
    raise RuntimeError("Fact migration ownership verification failed.")
context.configure(connection=connection, version_table=VERSION_TABLE, version_table_schema="public")
with context.begin_transaction():
    context.run_migrations()
