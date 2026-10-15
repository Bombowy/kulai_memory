# C2A: measured security performance and concrete VPS handoff

No production change is authorized by this document. C1 is already committed and
pushed as `f885ac202b5494873039fc80abe0788f3f5cab36`; its commit is not duplicated.
The full C2A measurements and test results are in `wynik_4d3c2a.txt` and
`docs/SECURITY_PERFORMANCE_4D3C2A.json`.

## What changed in the security path

`checkpoint()` combines the three metadata queries for canonical heads, database
name and OID into one SELECT. All values still come from the same repeatable-read
transaction as the complete security fingerprint and credential lookup. It never
retains a result between operations. Catalog validation, all six security tables,
gate-chain verification, credential expiry and revocation checks remain active.

Fingerprinting uses bounded 256-row partitions, retaining exactly the historical
column encoding, order, JSON canonicalization, length prefixes and SHA-256. The
owned-PostgreSQL regression compares every table against the old implementation,
crossing multiple partitions with 1000 synthetic rows. No schema, token or API
change is required. It also checks a data edit changes the fingerprint.

The burst test found an actual pre-existing Uvicorn integration error:
`handle_websocket_data` was not a hook used by pinned Uvicorn 0.54.0. The guard now
checks `send_receive_event_to_app`, which receives complete text, binary and
reassembled fragmented messages. Eight queued data messages trigger an abort on
the next message; remaining events in the same TCP batch are discarded. One
additional disconnect lifecycle event is allowed. mTLS/ASGI admission is unchanged.

## Reproduce on synthetic infrastructure

Use PyCharm's backend SDK (currently `backend/.venv/Scripts/python.exe`). Set
`KULAI_RUN_POSTGRES_INTEGRATION=1` only in the test process. Choose new absolute
output paths outside Git; certificates and gate files use disposable temporary
directories. Never use a real token or point these helpers at an unowned fixture.

```text
<BACKEND_PYTHON> -m backend.tests.security_stream_benchmark --label verification --seconds 20 --output <NEW_ABSOLUTE_JSON>
<BACKEND_PYTHON> -m backend.tests.journal_aging_benchmark --output <NEW_ABSOLUTE_JSON>
<BACKEND_PYTHON> -m pytest backend/tests/integration/test_security_hot_path.py -q
```

The stream benchmark covers 10 and 100 database sessions with 1/2/4 simultaneous
real TLS/WSS clients, unchanged 6400-byte PCM frames every 200ms and canonical
owned-DB saves. It records p50/p95/p99, scheduling and send latency, unfinished
frames, exact SQL counts, CPU/RSS and stop-to-saved. Observers delegate every live
authorization and never hold a principal cache. CPU is Python process CPU, not
whole-machine or PostgreSQL CPU. SQL counts come from SQLAlchemy's
`before_cursor_execute` event on the actual runtime engine; driver-internal
cursor FETCH/ping/transaction protocol exchanges are not a wire-level query
trace. Timing is a short loopback sample, not an SLO or
a ten-minute multi-client production test. 1000 rows are stress data beyond the
128-record pairing admission policy, never a supported device count.

## Journal aging and conservative review gate

The unchanged full-chain read is linear in journal history. Near 16 MiB it already
exceeds the 200ms frame cadence before SQL and lock contention. The test fills an
actual 16 MiB-limit fixture, verifies all records and checks refusal to append
without changing the file. Additional owned-DB tests cover corrupted/truncated
chains, rewind, concurrency, ambiguous COMMIT and no mutation when full.

The new read-only command is:

```text
<PYTHON_WITH_BACKEND> -m ops.remote_access.journal_status --gate <EXISTING_PROTECTED_GATE>
```

It checks real ACLs and the full chain under the existing mutex; it never creates
or repairs a journal, checks/mutates the DB, closes or releases the gate. Output
contains counts, bytes and one read duration, with no identities/checksums/token
data. A healthy journal alone remains `NOT VERIFIED` for database trust.

Deployment review now requires fresh measured `inventory.journal` evidence:
integer `records`, integer `bytes`, numeric `read_ms`. The conservative review
budget is at most 1000 records, at most 4 MiB and a measured read below 50ms.
Exceeding it gives BLOCKED; missing evidence gives NOT VERIFIED. This is an early
maintenance/review threshold, not a proven four-client capacity at that age or an
automatic runtime shutdown. Monitor before enabling traffic and periodically
during operation; do not wait for 16 MiB. Repeat concurrency measurements at the
actual journal age. Do not open deployment solely because these three numbers fit.

## Controlled journal replacement: full recovery, no compaction

There is no automatic rollover. Existing explicit recovery tools implement the
needed operations; a new owned-DB test exercises a new gate, actual backup and
restore verification, total credential invalidation, separate release and new
approved pairing while canonical data stays identical.

Each production step needs separate approval:

1. Stop public routing/bridge and every participating SECURE worker. Verify their
   ingress leases have drained; disable any automatic start. Close the old gate
   while there is space. If it cannot append CLOSED, keep process/network ingress
   physically stopped and treat it as an incident; do not edit or truncate it.
2. Create a complete verified security-v3 backup and manifest with an actual owned
   restore verification. Preserve the old journal read-only with its ACL/audit
   trail. Never roll it back together with the database.
3. Prepare a **new** protected external gate directory on independently retained
   storage, outside the repository/DB snapshot. Explicitly initialize it CLOSED
   using `scripts/security_recovery.py initialize`, with exact database confirmation.
4. Explicitly run `recover` against the current database and verified manifest.
   Existing recovery verifies the archive again and atomically revokes all issued
   access/session/refresh capabilities and unfinished pairings, retaining history.
   Do not blindly repeat COMMIT after an ambiguous result. Gate remains closed.
5. Verify canonical fingerprints, security schema and restored-credential denial.
   Separately approve `release`. Update **all** SECURE workers/launchers to the same
   new gate; retain the old closed directory. Never copy its OPEN state to the new
   file or run two active generations. Re-run launcher/readiness checks.
6. Approve every device's new pairing, then separately enable ingress. Test the
   old token, refresh and pairing proofs fail, and a newly approved device works.

Rollback stops ingress and leaves gates closed. Never restore old active
credentials or reopen the old journal to undo credential invalidation. An unknown
COMMIT requires explicit recovery from observed state. No automated script
provided here executes this production sequence.

## VPS handoff without guessing a host

No VPS/IP, domain, user or independently verified SSH host key has been supplied.
No SSH was attempted. Production values remain unknown, and no installable
candidate is generated from null fields. Existing C1 partial deployment.json
outside the repository remains a blocked handoff, not a production configuration.

The operator can run the single standalone collector locally on their Ubuntu VPS:

```sh
sh collect-preflight-safe.sh --self-test
sh collect-preflight-safe.sh --domain <CONFIRMED_DOMAIN> --wg-port <OPERATOR_SELECTED_UDP_PORT>
```

`--domain` optionally performs an ordinary system DNS lookup. Default collection
does no external IP discovery. No installs, sudo, service changes, network writes,
private-key reads, environment dumps, raw expanded Nginx or application logs are
used. Selected Nginx directives are summarized locally; unsupported includes,
effective firewall isolation, certificate ownership and host-key proof remain
NOT VERIFIED. Versions/listeners/route prefixes/WG handshake ages and available
ownership are redacted into JSON. The collector never substitutes its partial
scan for `nginx -t` plus a local expanded-config review.

For agent-operated diagnostics, supply exact SSH endpoint/user, selected domain,
the host-key fingerprint independently obtained in the provider console, and
explicit read-only authorization. Compare the key out of band, pin a dedicated
known_hosts entry, use `StrictHostKeyChecking=yes` and `BatchMode=yes`. Do not use
`accept-new`, `StrictHostKeyChecking=no` or guessed hosts. Stream the reviewed
script over stdin if authorized; do not upload arbitrary configs/logs into Git.

Administrator-local review must still confirm shared-IP/default ownership, the
complete expanded config, TLS/ACME ownership, includes, reserved map/zone names,
IPv4/IPv6 firewall rules, routes and service conflicts. Then choose actual /32s,
domain/SNI/SAN/EKU and accounts. Generate candidates in a **new external directory**
with `package.py`, validate using `validator.py` with fresh evidence and test the
combined Nginx configuration in isolation. Never install into `/etc/nginx`, take
over the existing default vhost or reload other projects in this task.

## PostgreSQL and next approval boundaries

See `POSTGRES_LOOPBACK_CHANGE.md`. Read-only observations found only loopback
clients, including other databases on the shared instance; this does not prove
scheduled/approved LAN clients are absent. Effective 5432 filters require a
privileged read. Until owner confirmation and that review, network isolation is
NOT VERIFIED and remote deployment is BLOCKED. Do not restart the instance or
edit `listen_addresses`, HBA or firewall based on the snapshot alone.

The C1 production sequence still applies: separately approved verified backup,
protected gate, security migration, PKI, tunnel, firewall, real two-host handshake,
SECURE ingress/bridge, shared Nginx integration, negative tests, public DNS/HTTPS,
pairing and physical LTE/5G smoke. Certificate/key operations, service changes,
firewall and DB actions each require explicit approval. Before any such step,
resolve the journal maintenance/capacity budget and PostgreSQL exposure checks.
