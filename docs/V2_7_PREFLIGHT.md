# V2.7 deployment checklist — preparation only

Read-only inspection on 2026-10-21. No cutover, production migration, APK
installation, configuration write or service reload was performed by V2.6.
The final test verdict and verification commit are recorded in `wynik.txt`.

## Observed running V1

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

Configuration SHA-256 before verification (recheck immediately before cutover):

| File | SHA-256 |
| --- | --- |
| `/etc/nginx/nginx.conf` | `bd51057afcaa8153f6be31fbdc221f430c55e7a392cfe33814739bdda36da651` |
| `/etc/nginx/sites-available/demo-kulai` | `13c4c234321fe46250e947bddcf3a6097813278ffdb6b7eaeb681161dc3deab1` |
| `/etc/nginx/snippets/kulai-memory-path.http.conf` | `c6f78b72826604a65f0cc42bfa1b4b68a2ced4f8408496b21f5b7b7b0b77d90d` |
| `/etc/nginx/snippets/kulai-memory-path.server.conf` | `0eeb3f0b13caa9dfd4b7be3784a248987ccd560b994cc81c03a4ef6dac8704cd` |

## Later V2.7, only after authorization

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
6. After backend/proxy checks pass, install the separately approved, verified APK
   without clearing application data, preserve pairing, and execute the phone checks.

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
