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
12. Optional persistent security tables are durable state, including trust,
    digests, expiry and revocation. Complete backup/restore fingerprints include
    them when installed; absence in historical backups remains explicit `None`.
13. Security uses its own Alembic version table/graph. It never upgrades canonical
    Memory implicitly. An absent security schema permits LOCAL_DEV compatibility;
    SECURE requires both schemas to pass strict validation before AI startup.
14. Installing security on main requires explicit confirmation, a retained full
    backup, actual restore verification, immediate archive/source revalidation,
    a transactional additive upgrade and strict post-upgrade doctor. Startup
    performs no DDL, and a failed upgrade retains its backup.
15. Security revision `kulai_security_0002` adds pairing sessions and a durable
    fixed-window request counter. Complete fingerprints cover every field of both
    tables, including proof digests and counters. Historical v1 security backups
    explicitly have `pairings=None` and `pairing_rate=None`; canonical-only historical
    backups still have `security=None`. Restore verification never invents records.
16. A guarded v1-to-v2 upgrade verifies unchanged canonical/device/token fingerprints
    in the DDL transaction. Writers, pairing clients and administrators must be stopped
    during the separately authorized installation. Restoring an older security backup
    can rewind revocation and proof counters; main restore therefore requires an explicit
    recovery decision and subsequent review/revocation of affected credentials.
17. Security v3 adds finite device sessions and consumed refresh digest history.
    Complete fingerprints include every session/refresh field; older schemas explicitly
    have `sessions=None` and `refreshes=None`. Guarded v1/v2-to-v3 upgrade preserves
    all existing device/token/pairing/rate fingerprints and the canonical head/data.
18. A database recovery may resurrect consumed or revoked credentials. Keep ingress
    closed until a separately authorized recovery procedure revokes restored active
    sessions and affected legacy access credentials. Require new pairing before
    reopening access; never equate a matching backup fingerprint with fresh authority.
17. Administrator-only expired-session pruning never removes a live trusted token's
    receiver capability. It revokes abandoned unclaimed devices and leaves durable
    device/token records intact. This is an explicit state change covered by backups;
    authentication, startup and ordinary snapshots never prune automatically.
