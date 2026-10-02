# Data safety contract

1. `memories` is the source of truth for durable user memories.
2. Embeddings, vector indexes, classifications, summaries, and future RAG chunks
   are derived data. They must remain rebuildable from source data.
3. An embedding-generation failure must never delete or invalidate a Memory.
4. A vector-store failure must never delete or invalidate a Memory.
5. Repositories do not hide `commit()` or `rollback()`; the caller owns the
   transaction boundary.
6. Destructive schema migrations require an explicit decision and separate
   review.
7. `Base.metadata.create_all()` is not a production schema-management path.
   Alembic migrations are the only supported path.
8. A future embedding model or dimension change requires an explicit reindex or
   rebuild. Incompatible vectors must not be mixed silently.
9. A backup is verified only after a successful restore test.
10. Diagnostics and integration tests must not leave fixture records behind.
11. PostgreSQL backups are restored only to a compatible major version. The
    canonical development and restore-test major is PostgreSQL 18.
