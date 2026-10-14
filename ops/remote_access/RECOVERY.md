# Security recovery gate (4D.3B.3)

Implemented in `security_recovery/gate.py`, `recovery.py` and
`scripts/security_recovery.py`. No schema migration was added. The existing
canonical v4 and security v3 catalogs are verified, not just version strings.

## Trust and persistence

The remote runtime now requires an externally provisioned gate. No gate, a closed
gate, a truncated journal, wrong database identity/fingerprint, missing schema or
uncertain COMMIT denies remote admission. An OPEN journal is never manufactured
by startup. LOCAL_DEV remains local and cannot be selected by the remote launcher.

Use a dedicated absolute directory outside this repository, PostgreSQL data and
backup directories, and outside any snapshot restored with PostgreSQL. Store it on
a persistent volume with independent recovery/backup policy. The example
`C:/ProgramData/KulAI-Memory/recovery` must be reviewed for the host's actual backup
scope; it is not proof of independence by itself. Only the service/operator account,
SYSTEM and Administrators may access it. Inheritance granting Users/Everyone access
must be removed by an explicitly authorized operator. The validator checks ACLs
without changing them. On Unix the directory must be owned by the service UID and
0700, its regular files 0600. Do not place the gate on a network share, synchronized
folder, ephemeral container volume, or untrusted parent directory.

Initialize as the final service identity, or explicitly transfer ownership/ACL
after approval. An individual operator ACE for a different user is not accepted by
the service runtime; authorized administration should use the Administrators group.

The append-only journal contains sequence, previous checksum, state, DB name/OID,
aggregate security digest and recovery evidence hashes. It contains no credentials,
pairing proofs or private content. OS permissions are its trust boundary; SHA256
detects corruption and is **not** a signature or tamper-proof monotonic hardware
counter. An administrator who rolls back both DB and journal can defeat this
software gate. A whole-host restore, uncertain disk durability or loss of the
independent journal therefore requires manual recovery using a newly initialized
CLOSED directory; never restore and trust an old OPEN journal. Keep the gateway
closed until recovery completes. Hardware/remote monotonic attestation is outside
this implementation.

Every supported security transaction takes a cross-process checkpoint lock, checks
the complete security fingerprint, and durably appends PENDING **before** SQL
COMMIT. Only an acknowledged COMMIT plus successful fsync permits a new OPEN
checkpoint. An ambiguous COMMIT or process/power failure leaves admission closed.
A completed rollback before COMMIT may restore the preceding OPEN state. Reads
verify the same snapshot without rewriting the journal. This serializes security
writes and scans all security rows: measure latency and retained-history growth
before production. There is no authorization cache.

The journal is bounded at 16 MiB and fails closed at capacity. There is no automatic
compaction. Monitor its size; planned rollover means stopped ingress, verified
backup, a new CLOSED gate, full credential recovery and manual release. Never
truncate/edit a journal to resume service. Old gates remain protected audit evidence.

Run **all** local SECURE processes and administration commands with the same
`KULAI_RECOVERY_GATE` absolute path (or explicit `--recovery-gate` for pairing admin).
Remote launchers require it explicitly. An uncoordinated SQL/admin writer causes a
fingerprint mismatch and shuts remote admission; it cannot silently reauthorize
restored credentials. Direct DB superuser access and tampering with the protected
gate are trusted operator powers, not an alternative bearer mechanism.

The runtime holds a shared OS lease for its lifespan; every security transaction
also holds a shared lease. Recovery/release/close require the exclusive lease and
refuse while ingress or participating writers are alive. Stop them first and
verify listeners/PIDs; a lock alone does not terminate in-flight application work.

## Operator workflow — explicit authorization required for real data

Use the configured backend SDK, not an arbitrary global Python. Commands below
are templates; replace `<...>` locally. They were not run on main. Default commands
are dry-run; changing actions require `--apply --confirm-database <EXACT_DB_NAME>`.
The database comes from the existing backend configuration; credentials stay there.

1. Disable the KulAI vhost/bridge and stop remote ingress plus every security writer.
   Do not stop or edit other projects. Keep ingress closed throughout DB recovery.
2. Obtain an independent verified full archive and evidence manifest. The `backup`
   command performs pg_dump, actual pg_restore to a capability-owned temporary DB,
   strict schema checks and full canonical/security fingerprint comparison. Keep
   archive/manifest protected outside Git. The restore probe needs CREATEDB and
   backup tools in an operator context; never grant these to a public HTTP endpoint.

   ```powershell
   <BACKEND_PYTHON> scripts/security_recovery.py backup --gate <ABS_GATE> --output <ABS_NEW_ARCHIVE> --manifest <ABS_NEW_MANIFEST>
   # After approval, same command plus --apply --confirm-database <EXACT_DB_NAME>
   ```

3. On first setup or independent-gate loss, provision a new CLOSED directory using
   `initialize`. Review ACLs and independent storage before proceeding. Never copy
   an old OPEN seal into place. During a controlled restore, `close` after stopping
   ingress persists CLOSED before any DB restore.
4. Restore the approved archive using the separately guarded DB restore procedure.
   Never run recovery as a migration substitute. Both actual catalogs must already
   match canonical `kulai_memory_0004` and security `kulai_security_0003`.
5. Run dry-run recovery, then explicitly approved recovery:

   ```powershell
   <BACKEND_PYTHON> scripts/security_recovery.py recover --gate <ABS_GATE> --manifest <ABS_VERIFIED_MANIFEST>
   # After approval: add --apply --confirm-database <EXACT_DB_NAME>
   ```

   Recovery appends CLOSED first, restores the archive again into a fresh owned DB,
   checks complete fingerprints and checks target canonical equality. One SQL
   transaction locks security tables and canonical writers, revokes **all** devices,
   access tokens and sessions, consumes outstanding refresh credentials, and makes
   every invited/pending/approved pairing terminal via its existing max-attempt
   rule. Row bindings, approvals, token digests and consumed refresh history remain
   for audit/replay detection. No canonical DML or schema change occurs.
   The result stays RECOVERED, still closed. Re-running completed recovery preserves
   timestamps/fingerprints. Failed or ambiguous COMMIT never triggers automatic retry.
6. Inspect the aggregate report and canonical/vector/tombstone fingerprints. Run
   the read-only deployment validator. Explicitly authorize `release` with the same
   database confirmation. Release checks schema, seal and absence of any usable
   recovered credentials/pairing before writing OPEN; it starts no listener.
7. Start ingress manually with SECURE, exact origin, mTLS inputs and this gate.
   Repeat negative TLS/auth/route tests. Approve **new** pairing for each device.
   The old receiver proofs and credentials remain unusable. A normal restart can
   reuse a matching OPEN checkpoint; recovery cannot automatically release one.

If recovery fails, keep gateway/bridge stopped and gate CLOSED/PENDING. Investigate
using redacted stage codes. Repeat the complete controlled verification/recovery
when authorized; never retry a possibly committed refresh or Memory write.

## Reproducible synthetic evidence

Set `KULAI_RUN_POSTGRES_INTEGRATION=1` and run the backend SDK:

```text
-m pytest backend/tests/test_recovery_gate.py backend/tests/integration/test_security_recovery.py -q
```

Tests create marker-owned DBs and temporary PKI only. The old-backup test first
demonstrates that restored DB state alone accepts the revoked bearer, then proves
gate rejection, real archive re-restore, atomic/idempotent recovery, old credential
denial and new approved pairing over real mTLS HTTPS. Canonical fixtures include
Memory, a 1024-dimensional vector and a tombstone. Fault tests cover corrupt archive,
mid-transaction failure, lost COMMIT response and checkpoint failure after COMMIT.
