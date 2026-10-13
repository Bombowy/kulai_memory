# Device session renewal — TASK 4D.3A

## Threat model and decisions (before implementation)

The device UUID, session UUID and credential UUID are public identifiers. An access
bearer permits only the approved Memory/assistant actions. A separate 256-bit random
refresh secret permits only rotation within its existing session. PostgreSQL stores
SHA-256 digests, never a recoverable credential. The device approval remains the
authority; refresh cannot add actions, change device or select a project.

Access lifetime is at most one hour (a shorter administrator pairing TTL is retained).
Refresh lifetime is 30 days, renewed only after a successful rotation and clamped to
the fixed 90-day session deadline. Ninety days is an absolute limit, not an inactivity
timer. A stolen refresh can therefore remain useful longer than a stolen access;
device/session revocation and mandatory rotation constrain this risk. These defaults
are deliberately finite. A maximum 4096 rotations bounds retained replay history.

The server serializes device/session rows across processes. A successful rotation
consumes the old refresh, revokes its access and commits the replacement together.
Reusing a correctly proven consumed refresh revokes the entire family, including all
its access tokens. Concurrent duplicates can therefore revoke a legitimate session;
Android must coalesce renewal and must not automatically retransmit a possibly sent
request. This follows the replay response described in [RFC 9700, §4.14.2](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14.2).

An invalid secret or mismatched public identity gets a generic denial and does not
revoke another session. Global persistent rate limits bound anonymous requests.
Request bodies are bounded, duplicate/unknown fields rejected, secrets occur only in
headers and encrypted client storage, and every response has `Cache-Control: no-store`.

A network failure during commit or response delivery is ambiguous. The server never
retries the transaction and cannot recover the emitted secret from its digest. Android
durably records renewal intent before sending. After a possibly sent failure or an
app restart with unfinished intent it requires pairing again. A connection failure
proven to precede request headers can retain the old credential and retry with bounded
backoff. No Memory write or NOTE is replayed to recover authentication.

Current ingress remains SECURE plus the validated local transport/Host policy. Refresh
gets one exact POST exception, never a prefix or a general issuance endpoint. HTTPS
through a cryptographically verified VPS ingress is a future deployment change.
IP addresses and proxy headers do not prove identity. Restoring an old PostgreSQL
backup can resurrect consumed/revoked credentials; recovery must revoke restored
sessions before serving traffic. Schema migrations never run on application startup.

Legacy security v1/v2 credentials retain their existing finite access-only behavior.
They acquire no refresh capability without new administrator-approved pairing.

## REST contract

`POST /api/security/refresh`, `Content-Type: application/json` (optional utf-8 charset),
`Authorization: Refresh <krf1_ plus 43 URL-safe characters>`.
The body is exactly three canonical lowercase UUID strings:

```json
{"session_id":"<session UUID>","device_id":"<device UUID>","refresh_id":"<refresh UUID>"}
```

No query, device/project claim headers, duplicate headers/JSON keys, extra fields,
redirects or bodies larger than 4096 bytes are accepted. The matching secret is not
part of the body. All replies use `Cache-Control: no-store`. Success is HTTP 200 with
exact fields: `device_id`, `token_id`, `access_token`, `expires_at`, `session_id`,
`refresh_id`, `refresh_credential`, `refresh_expires_at`, `session_expires_at`.
Times are aware UTC ISO-8601. The initial pairing claim on v3 returns this same
extended shape; v1/v2 access-only claim retains the four original fields.

Errors are fixed and never enumerate identities. 401 `code: renewal_required`
requires pairing. 429 `retry_later` and unavailable service/schema 503 `unavailable`
explicitly include `request_applied: false`; the valid old refresh may be used later
with backoff. SQL/commit/timeout failures after storage entry give 503 `uncertain`
without that assurance. Invalid contract yields 400 `invalid_request`; transport
policy denials retain the existing generic HTTP contract. Treat unrecognized gateway
errors, malformed success bodies and possibly sent network failures as ambiguous.
Never recover an emitted replacement by replaying the old secret.

The shared persistent limiter allows 120 security capability requests/minute globally.
A family cannot rotate before `min(30 seconds, access TTL / 2)` since its last issue;
at most 4096 generations are retained. Active/total pairing bounds remain 8/128.
These are application bounds; future public ingress needs independent abuse controls.
Existing session-linked access tokens cannot use the administrative access-only
rotation API to detach from their family. Legacy credentials retain that trusted API.

## Storage and lifecycle

Independent migration `kulai_security_0003` creates `kulai_security_sessions` and
`kulai_security_refreshes`; canonical head remains `kulai_memory_0004`. An unconsumed
refresh has a unique partial index per session; every refresh links its own access
UUID. Device/session/refresh/access row locks and a persistent global limiter serialize
cross-process operations. Commit precedes response; cancellation drains its short DB
transaction. Revoked devices fail both access and refresh checks. Session-aware lookup
adds family expiry/revocation to the existing coherent Authenticator reader.

Doctor checks complete columns, validated constraints, absence of triggers/RLS and
required index definitions. Full backup fingerprints cover every field, including
digests and consumption timestamps. Historical v1/v2 snapshots have explicit `None`
for absent session tables. Installation preserves all preexisting security/canonical
fingerprints. There is no main upgrade or startup DDL. Pairing receipts referenced by
a v3 session are retained even after expiry/revocation, preserving audit/replay linkage;
the 128-record bound can require a future explicit archival/retention procedure.

Android JSON storage version 2 reads version 1 and stores access+refresh+intent as one
AES-GCM AtomicFile replacement in noBackupFilesDir. It syncs file contents, the rename
directory entry, and verifies the saved ciphertext before publishing. IO/Keystore
work runs off the UI thread; CredentialProvider.current remains an atomic memory
snapshot. Server origin is part of the encrypted record and every RequestAuthentication
binds that exact origin. Invalid key/ciphertext/unfinished intent means pairing again.

Renewal is proactive before expiry, at most five minutes early (one quarter of the
remaining lifetime for short tokens). Concurrent requests join one bounded renewal.
The encrypted intent is saved before sending. OkHttp disables redirects and automatic
connection retry; a per-call event marker distinguishes a failure before request
headers from possible submission. Safe offline/service-limit failures preserve the
old credentials and try at 5/10/20/40/60 seconds, at most five automatic retries per
recovery episode. CHECK / RETRY permits another explicit attempt; a fresh app process
restores a new bounded recovery episode.
Timeout is 12 seconds for renewal; optional waiting callers are bounded to 15 seconds.
Uncertain outcome/failed replacement save leaves intent and locks private access.
No transport/ViewModel duplicates authorization or replays a Memory write. A delayed
denial from an old access request is ignored after a replacement has been published.

Current WS sessions keep their original bearer. During an active NOTE/voice ASK,
Android defers proactive rotation, rechecking at most every five seconds while the
access remains valid. Once voice finishes it renews; actual access expiry ends that
deferral. Rotation or revocation denies the predecessor's next private frame; the
transport reports failure without resending NOTE. This never extends access validity
or changes Voice Note v1/ingestion IDs. Test long audio and expiry under the future gateway.

## Tests and physical smoke procedure

Use the configured project interpreters; never point mutation tests at main:

```powershell
$env:KULAI_RUN_POSTGRES_INTEGRATION='1'
backend\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_sessions_postgres.py -q
backend\.venv\Scripts\python.exe -m pytest backend/tests --ignore=backend/tests/integration -q
.venv\Scripts\python.exe -m pytest tests -q
cd mobile/android
.\gradlew.bat testDebugUnitTest assembleDebug --console=plain
```

For an isolated physical test, from repository root run
`backend\.venv\Scripts\python.exe -m backend.tests.session_phone_fixture` with the
same opt-in. It creates a marker-owned temporary DB, installs both graphs only there,
uses synthetic Memory/AI and prints a random loopback PORT plus a private temporary
CONTROL directory. Administrative IPC is a local `command.json`, not HTTP. Commands:
`invite`, `pending`, `approve` (explicit session_id/device_id/full verification_code),
`statistics`, `revoke` (device_id), `finish`. The result file holds only a short-lived
test invitation or public status/counts; it never exports access/refresh secrets.

Install APK with `adb install -r` (no uninstall/pm clear). Map `adb reverse tcp:PORT
tcp:PORT`; select that exact loopback origin in SECURE. Enter the explicit invitation,
compare the receiver code, approve that device, wait for Paired. The fixture access
TTL is 60 seconds, so generation should increase after about 45 seconds. Verify
encrypted file permissions/size without exporting its content; force-stop/restart
the app and confirm the family remains usable. Remove only that test reverse mapping
temporarily, wait through refresh time, restore it and confirm bounded recovery and
generation advance. Revoke the device; CHECK/Memory must show refusal without extra
synthetic data calls. Unpair locally, restore the original LOCAL_DEV origin/ADB 8000,
send `finish` and verify owned DB/control cleanup. Remove UI dumps and temporary
invitation results. Never use these commands to modify main or real phone app data.

This smoke demonstrates credentials/Keystore/session recovery, not real RAG, Whisper
or local TTS. Device reboot and key-loss instrumented tests require a separate explicit
physical procedure; record them as NOT RUN if unavailable. Detailed actual results
and initial/final main read-only fingerprints are recorded in `wynik.txt`.

## Main installation gate

Only after separate user authorization: stop writers/clients/admin tools, run strict
doctor, create a retained full backup outside the repo, verify actual restore, then
use the guarded `scripts/security_schema.py upgrade --confirm-main-security-schema
--backup-output <new external .dump path>`. Revalidation precedes transactional DDL;
post-upgrade doctor and complete fingerprints must pass. Existing v2 devices stay
access-only; pair new devices to obtain sessions. Recovery from an old DB additionally
needs credential revocation as described in REMOTE_ACCESS_ARCHITECTURE.md. No step
in this document authorizes installation by itself.
