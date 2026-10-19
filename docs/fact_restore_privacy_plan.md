# Fact privacy after restoring an older backup

Plan only. TASK 5A.4 does not restore main, implement production recovery, create
an external ledger or grant production roles. A whole database backup includes
its old generation counters and can therefore resurrect a deleted source and
its formerly valid approval. Durable epochs alone cannot detect time travel of
the entire database. Logical DELETE does not erase WAL, replicas or backup media.

## Recovery barrier outside the restored snapshot

Before production, maintain an authenticated account-scoped retirement ledger
outside the backup's rollback domain. Record only technical source/account IDs,
monotonic event/barrier IDs, deletion time and authenticated audit identity.
Do not copy source text, fact claims, quotes, source hashes or free-text notes.
Protect this ledger against rollback, unauthorized reads and tampering, with a
reviewed retention policy. A missing/unverifiable ledger must block recovery of
fact retrieval and review; absence of evidence is not permission to reactivate.

The ledger's consistency and failure behavior need their own transactional or
durable outbox design. Do not promise deletion protection by writing a ledger
entry best-effort after acknowledging a source deletion. The ledger must also
cover account/device revocations and derived fact indexes when those exist.

## Explicit, reviewed recovery sequence

1. Restore into a newly verified owned/quarantined resource. Disable fact
   retrieval, approval, indexing and client access during recovery. Stop old
   application processes and invalidate their cached views/intents. Never restore
   an old dump directly into an active main database.
2. Verify backup authenticity, expected canonical/security/fact revisions and
   the independent ledger's current high-water mark. A v1 backup requires the
   separately reviewed v2 migration, never a startup migration. Do not treat
   successful `db_doctor` or matching old backup hashes as privacy authorization.
3. Replay authenticated retirements through a reviewed, owned-scoped recovery
   tool. Delete restored canonical sources retired after the backup, purge
   derived facts/quotes/relations and recreate only minimal technical retirement
   state for absent deleted sources. Include every future derived index. Use
   verified IDs and markers; never infer ownership from a DB name prefix.
4. Establish a fresh recovery barrier. A future reviewed tool can transactionally
   increment all surviving live source generations and invoke the same source
   invalidation routine, without changing source content or canonical revision.
   It must revoke all restored approvals/approved relations and append technical
   recovery audit. Never decrease/reset generations or reuse retired identities.
   Do not copy old APPROVED status into a new generation.
5. Require explicit new extraction/verification and human approval. Old request
   receipts are historical technical records, not commands to restore status.
   Reject stale client lock versions and cached source evidence. No automatic
   approval, merge or cross-device replay queue is authorized.
6. Check retirement coverage, absence of deleted content in all derived storage,
   strict doctors, full canonical/vector/tombstone/fact fingerprints and audit
   consistency. Test stale clients, retry/lost ACK, concurrent access, and a second
   crash during recovery. Resume read/review access only after explicit review.

## Before production

- Separate runtime privileges from table/trigger ownership. An administrator can
  bypass triggers or restore old data; this is not a normal application mutation.
  Review invoker/definer privileges for canonical-trigger updates to private fact
  tables, fixed search paths and the ability to disable triggers or TRUNCATE.
- Define ledger availability, integrity, replay idempotence, account isolation,
  storage erasure/retention and replica promotion requirements. Review legal
  retention of the minimal technical retirements.
- Test logical and physical restore paths and recovery barriers on disposable
  databases. These are future recovery tests, not performed automatically here.
- Keep the current production facts feature disabled until migration, privileges,
  audit, backup/fact fingerprints and client cache invalidation are reviewed.
