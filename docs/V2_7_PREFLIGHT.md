# V2.7 deployment record and rollback checklist

V2.7 was deployed on 2026-10-21 after the operator explicitly confirmed
`WDRAŻAJ V2.7`. V2.6 had performed read-only production verification only.
No APK was installed; physical Galaxy/LTE testing remains V2.8.
The current result is recorded in `wynik.txt`.

## Verified deployed V2

- Only `kulai_memory_mvp` was upgraded, 0004 -> 0005. Strict database doctor PASS;
  security remains 0003 and Recovery Gate OPEN/matching. Immediately after
  migration, full Memories/vector/tombstone/security fingerprints equaled the
  quiesced 0004 baseline. Legacy `kulai_memory` remains 0004 without CHAT tables.
- Full pre-migration archive, outside Git, never overwritten:
  `C:/Users/patry/AppData/Local/KulAI-Memory-Deploy/kulai_memory_mvp-pre-v2.7-20261021T023649Z.dump`.
  Size 46,404 bytes; SHA-256
  `f1480370da7beb786dd38f9332bbea7149e525b818a278a62e9fc0933ee52e71`.
  Existing backup/restore tools used pre-migration-from 0004. Actual restore
  fingerprints matched in full; the marker-owned database was removed before migration.
- Only the old Memory bridge PID 35288 was stopped, under the existing gate's
  checkpoint mutex. The same SECURE command/config now runs as PID 32348,
  started UTC `2026-10-21T03:12:45.3247371Z`, on loopback/WireGuard TCP 8443.
  No PostgreSQL/Ollama/other application service was restarted.
- Real VPS mTLS passed CA/SAN/client-certificate validation. Missing client
  certificate failed at TLS; missing app credentials returned 401. Before proxy
  activation, authenticated direct mTLS list/create/Qwen ask/history all passed.
- Only `/etc/nginx/snippets/kulai-memory-path.http.conf` changed: exactly the
  approved seven added lines. Its SHA-256 is now
  `7355fcd429b09d7cc307f25cb2fdf83ddcb895a3764fe9b639f251ee70575a64`.
  The other three configuration hashes below remain unchanged. `nginx -t` PASS,
  then one Nginx reload. All six demo statuses/body hashes matched immediately
  before and after reload; no rollback was necessary.
- Actual public HTTP/WSS passed: local-admin pairing/claim, text and real Whisper
  voice CHAT with Qwen/BGE, fresh-client history, pagination, identical UUID retries
  across HTTP/WS, two independent conversations, NOTE/MEMORY, classic text/voice
  ASK, a valid synthetic Memory citation, speech-plan and session refresh.
  Old access was denied after refresh; invalid methods/UUIDs/query names were denied.
- The separate synthetic verification device was revoked using local
  `pairing_admin.py`; phone credentials were never used or reset. Two clearly
  titled test conversations (5 turns/10 messages) and one synthetic NOTE/vector
  remain as verification data. Final strict doctor/gate PASS, zero owned test DBs.
- Protected evidence files use the prefix
  `C:/Users/patry/AppData/Local/KulAI-Memory-Deploy/v2.7-20261021T023649Z-`.
  Only their sanitized results belong in Git; no archive/credentials/audio do.

## Observed V1 before deployment

- VPS: `ubuntu@57.128.226.91`; existing HTTPS vhost
  `/etc/nginx/sites-available/demo-kulai` (enabled as `demo-kulai`).
- `/etc/nginx/nginx.conf` includes
  `/etc/nginx/snippets/kulai-memory-path.http.conf` once inside `http`.
  The existing HTTPS demo server includes
  `/etc/nginx/snippets/kulai-memory-path.server.conf` once.
- Current allowlist contains V1 only. `/memory/api/conversations` returns 404;
  anonymous `/memory/api/memories` returns 401. Existing `/memory/ws/assistant`
  is already allowed. Upstream `/memory/` stripping, verified mTLS and disabled
  automatic retries are present; `nginx -t` passed without reload.
- Actual VPS -> `10.77.77.2:8443` via `10.77.77.1`, CA/SAN validation and the
  existing gateway client certificate: HTTP 401 without application credentials.
- Windows: existing `remote_bridge.py --enable`, PID 35288, started
  `2026-10-20T20:15:55.7286080Z`; same protected config
  `C:/ProgramData/KulAI-Memory-Remote/config/ingress-bridge.json`.
  Public origin is `https://demo.kulai.eu` without a path. Recovery Gate is
  `C:/ProgramData/KulAI-Memory-Remote/recovery`: OPEN and matching the database.
- `kulai_memory_mvp`: canonical 0004, security 0003, no conversation tables.
  Legacy `kulai_memory` is also 0004 without conversation tables. Both were
  inspected using READ ONLY transactions. No production data was used in model tests.
- All six recorded V1 demo responses match both their saved statuses and body
  SHA-256: demo root 200, mentor 200, protected propertly 401, mrkacyk root 200,
  `https://mrkacyk.kulai.eu/printet3d/` 200, default `http://57.128.226.91/` 200.

Configuration SHA-256 before deployment (the old http fragment is retained in backup):

| File | SHA-256 |
| --- | --- |
| `/etc/nginx/nginx.conf` | `bd51057afcaa8153f6be31fbdc221f430c55e7a392cfe33814739bdda36da651` |
| `/etc/nginx/sites-available/demo-kulai` | `13c4c234321fe46250e947bddcf3a6097813278ffdb6b7eaeb681161dc3deab1` |
| `/etc/nginx/snippets/kulai-memory-path.http.conf` | `c6f78b72826604a65f0cc42bfa1b4b68a2ced4f8408496b21f5b7b7b0b77d90d` |
| `/etc/nginx/snippets/kulai-memory-path.server.conf` | `0eeb3f0b13caa9dfd4b7be3784a248987ccd560b994cc81c03a4ef6dac8704cd` |

## Reviewed V2.7 procedure

### Prepared phase 1, 2026-10-21 — approved with `WDRAŻAJ V2.7`

- Verified V2 commit: `9c1d92cd94e31fd1ff1eaa7939d61973e6dece97`, same feature
  branch and one checkout. All four active Nginx hashes above still match.
  The V1 PID/start time, schema 0004/security 0003 and OPEN gate still match.
- New immutable backup of those four Nginx files:
  `/var/backups/kulai-memory-v1/v2.7-20261021T023649Z-nginx.tar` and
  `C:/Users/patry/AppData/Local/KulAI-Memory-Deploy/v2.7-20261021T023649Z-nginx.tar`.
  Both SHA-256: `8981c17e7b5f34654463887c2f76b0d224f3f31dfaf3bfd121052d146b0281a3`.
- Proposed change touches only `/etc/nginx/snippets/kulai-memory-path.http.conf`:
  seven added lines allowing the documented conversation method/path pairs.
  Pagination names are restricted; existing API validation checks values and
  paired list cursors. The server fragment, includes, TLS and other routes stay as is.
  Exact diff: `C:/Users/patry/AppData/Local/KulAI-Memory-Deploy/v2.7-20261021T023649Z-nginx.diff`.
  Diff SHA-256: `b8509cbdeb409cb55c69e2df53ac4248f2bd7b7bf2af48622d175577ab66faf9`.
  Candidate SHA-256: `7355fcd429b09d7cc307f25cb2fdf83ddcb895a3764fe9b639f251ee70575a64`.
  The adjacent `-manifest.json` records guarded paths/hashes; recheck before use.
- Real isolated TLS/mTLS Nginx tests: 7 PASS, including valid pagination,
  prefix removal, invalid UUIDs/paths/methods/queries and the existing V1 behavior.
  All six live demos match the saved V1 status/body hashes. No active config changed.
- Planned new database archive (not created before approval):
  `C:/Users/patry/AppData/Local/KulAI-Memory-Deploy/kulai_memory_mvp-pre-v2.7-20261021T023649Z.dump`.
  Its existing parent directory passed PRIVATE ACL validation. Never use `--force`.
  After stopping only Memory writers/bridge, take a fresh snapshot, run existing
  `scripts/db_backup.py --output <that exact archive> --pre-migration-from kulai_memory_0004`,
  then `scripts/db_restore_smoke.py <that exact archive> --pre-migration-from kulai_memory_0004`
  with the backend interpreter. Require equal full fingerprints and owned DB cleanup.
  Recheck `current_database() = kulai_memory_mvp`, source 0004 and the sole local
  head 0005 immediately before the existing Alembic upgrade; keep security at 0003.
- Restart only the existing bridge command with its current protected JSON and
  backend interpreter. Require strict doctor, unchanged canonical/security
  fingerprints and OPEN gate before reopening Memory. Then apply only the hashed
  diff, test/reload Nginx and repeat all six demo checks. A separate synthetic
  verification client will exercise authenticated pairing/refresh, NOTE/MEMORY/ASK,
  speech-plan and text/voice CHAT without using the phone's credentials or private text.
- Estimated interruption: approximately 5–10 minutes for Memory only, longer if
  a critical check fails. Stop further changes on failure. Database downgrade/full
  restore requires a separate decision as described below; never start V1 on 0005.

### Approved-window procedure

1. Agree on the brief Memory maintenance window and rollback, retain the V1
   commit `fbf77dae3e96efbf4f71084f73da2eac3f7b6f16`, protected bridge settings,
   Recovery Gate and APK. Back up the actual Nginx files outside Git and capture
   statuses/body hashes for all existing demos before any configuration write.
2. Drain Memory writers, pairing and refresh; stop only the existing Memory
   bridge during the approved window. Create a full backup using the existing
   `db_backup.py --pre-migration-from kulai_memory_0004` mechanism and verify its
   restore into a marker-owned temporary database. Retain/revalidate the archive.
3. Apply only canonical migration 0004 -> 0005 to `kulai_memory_mvp`: the three
   conversation tables, constraints and indexes. Keep security at 0003. Require
   strict doctor PASS and unchanged Memories/vector/security fingerprints.
4. Validate and start the existing bridge with V2 code, same SECURE config,
   database identity, certificates and WireGuard binding. Do not initialize a
   new Recovery Gate or run recovery/release for this additive canonical migration.
   Confirm real mTLS and existing V1 authentication before reopening Memory.
5. Present the exact Nginx diff for approval. Extend only the Memory allowlist:
   GET/POST `/memory/api/conversations`, GET
   `/memory/api/conversations/{UUID}/messages`, POST
   `/memory/api/conversations/{UUID}/ask`. GET list/history must allow their
   existing pagination queries; UUID matching and all other restrictions stay
   strict. See `CONVERSATION_API.md`. After approval and hash recheck: write only
   the approved files, `nginx -t`, then reload only Nginx on PASS. Compare all demos.
6. Leave APK installation to separately authorized V2.8. Preserve application
   data/pairing and execute the phone checks then; V2.7 does not install an APK.

## Rollback decision

Close only Memory ingress on a critical failure, retain diagnostics, and restore
only the changed Memory Nginx fragment(s) from verified copies; test before reload.
Other applications, domains and services remain untouched.

The pinned V1 code expects canonical 0004; switching code alone against 0005 is
not a complete rollback. Before any database rollback, stop Memory writers and
retain a verified full **0005** archive containing new conversations and current
Memories/sessions. A separately reviewed/authorized Alembic downgrade to 0004
removes only the three conversation tables; the owned-database test verifies
that canonical Memories, vectors and security remain unchanged. This discards
live CHAT tables, so retain their verified archive and require explicit approval
before doing it. Then validate/start pinned V1 with the original SECURE config
and confirm gate/authentication and all old flows. No automatic downgrade/reset.

Do not casually restore an old full security snapshot: it can resurrect consumed
or revoked credentials. If full database restore is chosen instead, use the
existing authorized recovery/revocation/release procedure before reopening ingress.

## Galaxy S25 Ultra after deployment

1. Preserve pairing and `https://demo.kulai.eu/memory/`, SECURE mode; test on LTE/5G
   with USB disconnected and no ADB reverse.
2. NOTE voice -> Saved -> MEMORY read; classic text/voice ASK with valid citations.
3. CHAT: new conversation, several alternating text/voice follow-ups, second
   independent conversation; switch conversations during a pending request.
4. Fully close/reopen the app, fetch the server list and continue existing history.
5. PL/EN SPEAK, STOP AUDIO and auto-read; no metadata read aloud, no automatic
   replay of old history. Check layout/keyboard and exclusive NOTE/ASK/CHAT microphone.
6. Interrupt the connection, retry the original request UUID and confirm one pair;
   unauthorized client is denied. Verify PC-unavailable behavior and explicit
   reconnect/retry without automatic repetition of writes.
