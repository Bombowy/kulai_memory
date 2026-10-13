# Local Memory history, assistant and speech planning transport

Start with `.venv\Scripts\python.exe scripts\dev.py`. It binds exclusively to
`127.0.0.1:8000`; the named dev launcher explicitly selects `LOCAL_DEV` when no
security mode is configured. An existing `KULAI_SECURITY_MODE=SECURE` in environment
or `backend/.env` remains secure. History/detail return canonical user data only
after the common access policy. Do not expose the port to a network. Local pairing
is described below; LAN/WSS/VPS deployment remains future work. No CORS wildcard is added.

Desktop stays in process. The server performs no audio synthesis and stores neither questions
nor answers. It uses existing canonical RAG grounding/citation rules, with no
reviewed semantic score cutoff or reranker. No database migration is required.

## History

`GET /api/memories?archived=false&limit=50`

- `archived=false` returns only active Memories (default); `true` only archived.
- `limit` is 1..100 (default 50).
- `before_created_at` (timezone-aware ISO 8601) and `before_id` (UUID) must both be
  supplied or both omitted. No OFFSET pagination is used.
- Order is `created_at DESC, id DESC`. The exclusive cursor condition is
  `created_at < before_created_at OR (created_at = before_created_at AND id < before_id)`.
- The repository fetches `limit+1`; a following page is indicated by a cursor
  containing the last returned row. `next_cursor=null` means no following page.
- Each request has its own read-only snapshot; pages are not a frozen snapshot
  across separate requests. Preview collapses whitespace with one space and then
  takes at most 200 characters; it is never written to the DB.

```json
{
  "items": [{
    "id": "00000000-0000-0000-0000-000000000001",
    "created_at": "2026-01-01T12:00:00Z",
    "revision": 3,
    "archived": false,
    "content_preview": "Synthetic memory preview"
  }],
  "next_cursor": {
    "before_created_at": "2026-01-01T12:00:00Z",
    "before_id": "00000000-0000-0000-0000-000000000001"
  }
}
```

`GET /api/memories/{memory_id}` reads either active or archived canonical detail.
Missing/hard-deleted Memory returns 404, `{"detail":"Memory not found."}`.

```json
{
  "id": "00000000-0000-0000-0000-000000000001",
  "created_at": "2026-01-01T12:00:00Z",
  "revision": 3,
  "archived": false,
  "content": "Full synthetic canonical content"
}
```

Neither response includes ingestion/session IDs, arbitrary metadata, vectors or
provider diagnostics. Reads use short PostgreSQL READ ONLY snapshots without
model calls; Desktop Library/Recent semantics are unchanged.

## Text question

`POST /api/assistant/ask`, JSON body:

```json
{"query":"Gdzie mieszka zielony smok?","top_k":5}
```

Query must be nonblank and at most 10,000 characters; top_k is an integer 1..20,
default 5. The runtime calls `MemoryRagRuntime.ask`, using shared BGE and Qwen.

Send JSON as UTF-8. Windows PowerShell 5.1 can encode a string body using a
legacy encoding when no charset is specified. For Polish questions use:

```powershell
$body = @{ query = "Gdzie mieszka syntetyczny zielony smok?"; top_k = 5 } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/assistant/ask" `
  -Method Post -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

No keepalive setting, second request or model warmup is required. Invalid UTF-8
JSON is rejected before RAG with 400 and `Invalid assistant request.` rather than
an internal 500. The adapter catches Starlette's base HTTPException as well as
FastAPI subclasses; diagnostic logs contain only stage/type/allowlisted code.

```json
{
  "status": "answered",
  "answer": "Na Wenus.",
  "citations": [{
    "memory_id": "00000000-0000-0000-0000-000000000001",
    "rank": 1,
    "score": 0.7395
  }]
}
```

Insufficient evidence returns:

```json
{"status":"insufficient_context","answer":"Nie mam wystarczających informacji w pamięci.","citations":[]}
```

Citations come exclusively from validated canonical RAG results. No prompt,
retrieval context, source content, vectors or model payload is returned. Zero
retrieval hits skip Qwen. Queries/answers produce no Memory/vector/tombstone writes.
HTTP disconnect/task cancellation cancels RAG and releases DB sessions.

Bounded public errors have only `detail`:

| Status | Message |
| --- | --- |
| 401 | Authentication required. |
| 403 | Access denied. |
| 400 | Invalid assistant request. |
| 422 | Invalid assistant request. |
| 404 (detail) | Memory not found. |
| 503 | Memory assistant is unavailable. |
| 500 | Memory assistant could not complete the operation. |

Validation never echoes query/input and errors contain no content/credentials.
Host logging records only transport/action/status, never URLs, client headers,
bodies or Memory content. The dev launcher disables access logs and informational
WebSocket URL logs; proxy header interpretation is also disabled.

## Final-answer Polish / English speech plan

`POST /api/assistant/speech-plan`, UTF-8 JSON:

```json
{"answer":"To jest odpowiedź. This is an answer."}
```

Answer is nonblank and <=6,000 characters. Only the final answer is supplied;
there are no query/citation/Memory ID fields. Response:

```json
{"segments":[{"text":"To jest odpowiedź. ","language":"pl"},{"text":"This is an answer.","language":"en"}]}
```

Both projections forbid extra fields. There are 1..64 nonempty segments,
languages only `pl`/`en`, with concatenation exactly equal to the requested
answer. No translation/rewrite/fuzzy repair or whole-answer Polish fallback.
This endpoint reuses `MemoryRagRuntime.plan_speech`/canonical application speech
planning through the server AI lane and the existing Qwen client. The canonical
insufficient answer yields one PL segment without a Qwen request. Planning
opens no DB session, invokes no BGE/Whisper and creates no audio artifact.
The answer is untrusted DATA; speech planning does not change RAG grounding.
HTTP disconnect cancels planning and leaves the runtime reusable.

Safe errors: 400/422 `Invalid speech request.`, 503
`Memory assistant is unavailable.`, 500 `Speech planning could not be completed.`
Neither logs nor errors contain answer/prompt/provider payload. Android performs
its own exact-plan validation and local offline synthesis; voices and audio are
not transported by the server. `/ws/memory` and `/ws/assistant` v1 are unchanged.

## Voice question v1

`ws://127.0.0.1:8000/ws/assistant` accepts one voice question per connection.
It has a separate command/result contract from voice notes. No ingestion UUID
is accepted or allocated.

```json
{
  "schema_version": 1,
  "type": "question.start",
  "audio": {"encoding":"pcm_s16le","sample_rate":16000,"channels":1},
  "top_k": 5
}
```

After `session.ready`, send binary signed PCM16 little-endian mono frames at
16 kHz, then:

```json
{"schema_version":1,"type":"question.stop"}
```

The existing audio contract/limits are reused: each frame <=256 KiB, even byte
length; total <=19,200,000 bytes (10 minutes). The server owns a private temporary
WAV, drains Whisper before deleting it, and removes it before RAG. Recording,
invalid protocol, disconnect, error, cancellation and shutdown all clean owned audio.

Every server event has this envelope:

```json
{"schema_version":1,"type":"session.ready","session_id":"00000000-0000-0000-0000-000000000002","sequence":1,"payload":{}}
```

`session_id` is a random server UUID, sequence starts at 1 and increments by 1.
Normal event order/payloads:

1. `session.ready`: `{}`
2. `transcript.final`: `{"text":"recognized question"}`
3. `rag.started`: `{}`
4. `assistant.completed`: exactly the text-question response payload above.
5. Normal socket close, 1000.

No `rag.context` event is emitted. Empty audio emits an empty transcript without
STT; empty/silent transcription closes 1000 after `transcript.final`, without
`rag.started`, BGE query or Qwen calls. No ingestion/indexing is run for questions.

An `error` payload is `{"code":"...","message":"...","recoverable":false}`:

| Close code | Reason |
| --- | --- |
| 1002 | Invalid command/audio/order, including frames during processing |
| 1009 | Frame or total audio size exceeded |
| 1013 | Runtime unavailable |
| 1011 | Internal/STT/RAG failure, with safe public error |

Disconnect monitoring remains active during Whisper/RAG. Thread-backed Whisper
drains before audio removal; network BGE/Qwen cancels promptly. No stale completed
event is sent after an observed disconnect.

## Runtime and unchanged voice notes

One lifespan owns one `VoiceMemoryServerRuntime`: one DB engine/pool, one Whisper
provider/model, one shared BGE client, one indexer borrowing BGE, and one
`MemoryRagRuntime` borrowing BGE and owning one Qwen client. No Qwen generation
runs at startup. Partial startup failure cleans already-created resources.

One AI lane serializes Whisper, BGE indexing/query embedding and Qwen. Voice Note
releases its STT lane before canonical save, closes the transaction, then enters
the lane for indexing. Voice Question releases STT and its WAV before entering
RAG. RAG opens only its existing short retrieval snapshot and closes it before
Qwen. History/detail do not acquire the AI lane, so they can run while models are
busy. Shutdown cancels RAG, drains Whisper, closes Qwen/indexer/shared BGE once,
disposes the engine and releases CUDA scope.

`/ws/memory` v1 remains unchanged: `recording.start` with stable ingestion UUID,
binary frames, `recording.stop`, optional `memory.retry`; events are
`session.ready`, `transcript.final`, `memory.saving`, `memory.saved`, `error`.
Save retry uses the same transcript/IDs without repeated STT. Current Android
Voice Note continues to use this endpoint.

## Android local transport

- HTTP_BASE_URL (debug): `http://127.0.0.1:8000`
- VOICE_NOTE_WS: `ws://127.0.0.1:8000/ws/memory`
- VOICE_ASSISTANT_WS: `ws://127.0.0.1:8000/ws/assistant`

Continue `adb reverse tcp:8000 tcp:8000` for a USB-connected debug device.
Android 6C.2/6C.3 supplies history/detail, text/voice assistant and local PL/EN TTS.
There are no lifecycle mutation endpoints or persisted assistant conversations.

## Authentication foundation (TASK 4D.1)

The neutral `kulai_memory.security` package has no Memory/RAG, FastAPI, database
or UI dependency. Device UUIDs and token UUIDs are public identities. Credentials
are independent 32-byte CSPRNG secrets from Python `secrets.token_urlsafe(32)`;
only SHA-256 digests enter storage. This is random-token hashing, not a password
scheme. Immutable records validate UUIDs, aware timestamps, lifetime and actions.
Pending devices cannot receive credentials; revoked devices cannot be re-trusted.
Token validity is `issued_at <= now < expires_at`, with default TTL one hour and
maximum legacy administrative issuance TTL 30 days. TASK 4D.1 has no refresh API;
TASK 4D.3A adds a separate finite session capability, described below.

`SecurityReader.lookup` reads a coherent token/device snapshot. `SecurityStore`
defines device trust/revocation, token creation/revocation and atomic rotation.
Rotation preserves device, actions and project, revokes the predecessor and inserts
one successor atomically. The included `InMemorySecurityStore` is a process-only
reference/test implementation. Restart loses its state. TASK 4D.2A supplies the
persistent PostgreSQL adapter; TASK 4D.2B supplies administrator-approved pairing
and Android encrypted storage, described below. Their security migrations remain
independent of the canonical Memory migration graph and require explicit installation.
The host borrows the reader, and never closes externally owned storage.

| Mode | Private operations | Boundary |
| --- | --- | --- |
| `LOCAL_DEV` | Missing credentials explicitly permitted; supplied credentials still validated | Numeric loopback listener and peer only |
| `SECURE` | Bearer credentials required; missing/broken store, timeout or invalid state denies access | Loopback listener remains required in this phase; IP never proves device identity |

`Settings` and `SecurityConfig` default to `SECURE`. The `.env.example` explicitly
selects `LOCAL_DEV`. The named dev launcher selects local compatibility only when
neither environment nor `.env` defines a mode. It respects configured `SECURE`.
An intentional CLI override is `scripts/dev.py --security-mode LOCAL_DEV|SECURE`.
The launcher always binds `127.0.0.1`, keeps ADB reverse, disables proxy headers,
access logs and informational WS URL logs. No LAN bind option is added.
Embedding the ASGI app in another server requires equivalent listener/logging/proxy
configuration; application middleware cannot remove raw logs produced upstream.

HTTP and native WebSocket clients send `Authorization: Bearer <opaque token>`.
The optional `X-Kulai-Device-ID` must be a canonical lowercase UUID matching the
credential owner. Omitting that claim still authenticates the token's registered
device. This is bearer authentication, not cryptographic proof of physical device
ownership: possession of a stolen valid bearer token can impersonate that device.

| Route | Required action |
| --- | --- |
| `GET /api/memories` | `memory.read` |
| `GET /api/memories/{id}` | `memory.read` |
| `POST /api/assistant/ask` | `assistant.ask` |
| `POST /api/assistant/speech-plan` | `assistant.speech_plan` |
| `/ws/memory` | `memory.write` |
| `/ws/assistant` | `assistant.ask` |

ASGI enforcement precedes routing, redirect generation, body validation, runtime
access, audio allocation and AI operations. Unknown private operations are denied
by default in SECURE. `/health` remains exactly public GET liveness `{"status":"ok"}`.
Tokens in query parameters, cookies or WS subprotocols never authenticate. Known
credential query keys (including encoded keys) and all WS query parameters are
rejected. Duplicate/malformed Authorization and identity headers are rejected.
Untrusted Host/Origin and forwarded headers are refused. Native clients have no
Origin; browser clients need a later explicit origin policy.

HTTP authentication errors are generic 401 with `WWW-Authenticate: Bearer` and
`Cache-Control: no-store`; policy/transport denials are generic 403. An unauthorized
WS sends `websocket.close` before accept: real Uvicorn returns HTTP 403 without a
session/event/body containing Memory data. Established WS credentials are checked
again before each incoming private frame, including stop/retry; expiry/revocation
closes with code 1008. Authorizations already admitted to an operation are not
retroactively rolled back; an idle WS does not have a revocation push/timer yet.
A committed note survives an indexing disconnect under existing idempotency rules.

Authentication has a five-second timeout. While storage awaits, one receive watcher
monitors disconnect, buffers at most 64 messages/64 KiB, and replays original input
after success. Disconnect/cancellation drains both watcher and lookup tasks, without
starting endpoint work. Normal operations retain their existing cancellation rules.
Voice Note now also monitors disconnect during processing and save retry; shielded
Whisper drains before WAV removal, network inference cancels and owned resources
close once. Authorized REST projections and voice protocols remain unchanged.

The shared action/project policy prepares future resource authorization. Current
routes address the existing global canonical Memory, so project-scoped tokens and
client-selected project headers cannot access them. Future project adapters must
resolve the actual resource project server-side; the header alone is not authority.
There is no row isolation, account model or per-device ingestion namespace yet.

The host replaces vendor request logging/fallback handlers at composition, without
modifying the submodule. Logs contain fixed transport/action/status or the existing
safe operation stage/type/code; no URL, header, caller request ID, body, digest,
credential or private exception payload is logged. Debug tracebacks are disabled.
Android uses the same header policy for all HTTP operations and both WS handshakes,
binds credentials to one origin and disables redirects. Debug opts into LOCAL_DEV;
release defaults to SECURE; TASK 4D.2B supplies an origin-bound Keystore-backed
provider. A fresh SECURE client has no credentials until pairing completes.

The security integration suite uses real Uvicorn/HTTP/WS over random loopback ports
and controlled providers/storage. It performs no main DB writes, migrations or
real model calls. Existing opt-in PostgreSQL/model suites remain separate.
The pre-accept denial follows the [ASGI WebSocket specification](https://asgi.readthedocs.io/en/latest/specs/www.html#close-send-event);
token generation follows [Python secrets](https://docs.python.org/3/library/secrets.html).

## Persistent PostgreSQL Security Store (TASK 4D.2A)

The default SECURE composition now uses `PostgresSecurityRuntime` and
`PostgresSecurityStore`; explicit reader/authenticator injection remains supported.
The core interfaces/models are unchanged and do not import persistence, Memory or
RAG. The lifespan validates canonical and security schemas before starting any AI
runtime. A missing/incompatible schema keeps public liveness and denies private
operations. There is no DDL, token bootstrap or dev fallback during startup.

One owned security pool serves short transactions (`echo=False`, hidden parameters,
4-second statement/3-second lock timeouts). Lookup uses a READ ONLY transaction and
one coherent token/device JOIN. Sessions close before endpoint/audio/AI work.
Voice resources close before the security pool; disposal drains on cancellation.
Borrowed readers are not closed by the app. No application authorization cache is
introduced; prepared SQL statement caching does not cache credential state.

The independent graph is `backend/security_alembic.ini` with its initial revision
`kulai_security_0001` and `public.kulai_security_version`; TASK 4D.2B extends its
head to `kulai_security_0002` while accepting valid v1 backups. The canonical graph/head
stays `kulai_memory_0004`. Devices/tokens live in `public.kulai_security_devices`
and `public.kulai_security_tokens`, with UUID keys, owner FK, unique SHA-256 digest,
aware timestamps, validated lifetime/status/action array and nullable project UUID.
No plaintext token or metadata field is stored. Schema validation checks exact
columns, constraints, index, revision, and refuses partial schemas, extra security
tables, user triggers and row security. Project-scoped tokens remain refused by
the existing global Memory routes; no multi-project row isolation is claimed.

All writes lock device before token. Concurrent rotations of one old token produce
one successor. Rotate vs revoke is serialized: revoke winning first blocks rotate;
rotate winning first may create a successor before revoke of the old token.
The old token is revoked in both outcomes. Revoke an entire device to invalidate
all its credentials, including a successor that linearized first. Issue vs device
revoke is also serialized; even an issue that commits first becomes unusable when
device revoke commits. Revoked devices cannot be re-trusted under the v1 policy.
These are PostgreSQL row locks, including across independent processes, following
the [PostgreSQL locking contract](https://www.postgresql.org/docs/18/explicit-locking.html).

Mutations return only after commit. Failures/cancellation roll back and close the
session; driver/commit/rollback/dispose exceptions have safe authored errors.
No automatic write retry is used. A lost connection during actual COMMIT can leave
the caller uncertain about completion; inspect durable state after reconnect.
The implementation does not claim to undo a commit already acknowledged by PG.

Complete database snapshots now carry optional security revision and aggregate
device/token hashes/counts, covering every persisted field without printing rows
or raw digests. Old snapshot constructors default security to `None`; historical
0002/0003/0004 dumps without these tables still verify through existing guarded
backup/restore. An empty installed schema is distinct from absence. Any security
mutation invalidates an earlier verified-source token. Existing backup/deletion
safety checks remain mandatory; unknown/broken security schema fails strict doctor.

Read-only checks (safe to execute on current main):

```text
.venv\Scripts\python.exe scripts\db_doctor.py
.venv\Scripts\python.exe scripts\security_schema.py heads
.venv\Scripts\python.exe scripts\security_schema.py check
```

`check` requires the security schema and returns UNAVAILABLE before installation.
The current main database has not been upgraded. Installation is a separate,
reviewed administrative action after stopping writers and reviewing this change:

```text
.venv\Scripts\python.exe scripts\security_schema.py upgrade --confirm-main-security-schema --backup-output C:\path-outside-repo\new-security-before.dump
.venv\Scripts\python.exe scripts\db_doctor.py
.venv\Scripts\python.exe scripts\security_schema.py check
```

The upgrade creates a retained full dump, verifies an actual restore into an owned
temporary DB, revalidates source/archive immediately before DDL, serializes schema
installation with an advisory lock, compares canonical fingerprints before/after
within the transaction and requires post-upgrade strict doctor. It changes no
existing Memory/vector/tombstone records. Failure rolls back transactional DDL and
retains the archive. Direct Alembic execution without the guarded connection is
refused; destructive security downgrade is not implemented. Restart the backend
after an approved installation. Pairing/issuance UI and public token endpoints are
outside 4D.2A; credential management remains a trusted host API.

The measured synthetic loopback audio test sent 128 binary frames: 131 lookups
(handshake + start + frames + stop), 262 SQL statements including READ ONLY setup,
about 204–294 ms total across validation runs, with zero pool checkouts on completion
and during AI callbacks. Counts use SQLAlchemy statement events for adapter SQL;
driver BEGIN/COMMIT and pool pre-ping housekeeping are excluded from that count,
but their time is included in the end-to-end measurement.
Android's current normal 3,200-sample frame is 200 ms at 16 kHz, about 5 frames/s
and 10 authentication SQL statements/s. This supports retaining fresh per-frame
checks in the current local phase. No cache optimization is necessary for this
measurement; larger deployments need their own measurements. Idle sockets and
already admitted operations retain the revocation semantics documented in 4D.1.
# TASK 4D.2B: threat model parowania (przed implementacją)

Trust boundaries: local administrator CLI, untrusted native client, ASGI transport,
pairing application, PostgreSQL transaction, Android Keystore. The administrator
may explicitly reveal a short-lived invitation; only a specific session/device can
be approved. Neither device UUID, device name nor client IP proves identity.

An invitation uses 256 random bits and expires in five minutes. The phone generates
an independent 256-bit receiver secret, persists it encrypted before registration,
and proves possession on status/cancel/claim. Only digests enter PostgreSQL. The
first accepted registration binds the invitation to that receiver and a newly
allocated PENDING device. Exact retries are idempotent; a different receiver cannot
reuse the invitation. Administrator approval locks that session and device. Claim
locks them again, checks expiry/trust and atomically issues one bounded access token
and consumes the session. No plaintext token is retained for replay. If the claim
response or local durable save is lost, re-pair and revoke the orphan device.

Threats considered: stolen invitation, guessed public identifiers, receiver
substitution, replay, approve/claim/revoke races, multiple backend processes,
malformed/oversized inputs, credential logging, backup export, lost Keystore key,
cross-origin requests and redirection, storage faults and disconnects. A stolen
invitation can occupy a pending session; the administrator must compare session,
device identifiers and receiver verification code shown by both CLI and phone.
It cannot bypass explicit approval. Failed proofs have a durable attempt budget;
global fixed-window and active/total session limits bound abuse across processes.
Attackers can exhaust a known session's budget (availability loss, never trust).

Only exact native POST pairing routes bypass bearer authentication, and only in
SECURE on actual loopback listener AND loopback peer, with no Origin/proxy headers,
query parameters or redirects. This is a transport restriction, not device identity.
VPS/HTTPS requires a future explicit trusted ingress policy; headers cannot enable
it. No public deployment is introduced here. LOCAL_DEV remains explicitly local.

Android uses AES-GCM with an Android Keystore key and atomic encrypted files in
noBackupFilesDir. Credential snapshots bind to a canonical configured origin;
external origins require HTTPS. Platform certificate/hostname verification remains
enabled. No device attestation or hardware-backed-key guarantee is claimed.
Access tokens keep the existing one-hour default and maximum bounded lifetime.
The original 4D.2B contract has no refresh secret: its expired access-only credentials
require pairing again. TASK 4D.3A extends new pairings on security v3 with rotating
refresh capabilities; existing v1/v2 credentials are never silently upgraded.

## Pairing contract and administration (TASK 4D.2B)

Pairing is disabled in LOCAL_DEV. SECURE needs verified canonical and security v2
schemas (or verified v3 for session credentials). A missing/v1 schema never implicitly
creates or upgrades tables. These exact POST routes are pre-auth exceptions, with the local ingress checks
specified in the threat model. Content-Type is JSON, optionally UTF-8 charset;
duplicate headers/JSON keys, unknown keys, queries and bodies over 4096 bytes are
refused. Browser Origin and all Forwarded/X-Forwarded headers are refused. Native
proofs use `Authorization: Pairing <43-character secret>`. Responses use no-store;
invalid state/proof has one generic error without device enumeration.

| Route | Proof | JSON body | Result |
| --- | --- | --- | --- |
| `/api/pairing/request` | invitation secret | `session_id`, `receiver_secret`, `client_id`, `device_label` | Bound PENDING view; exact retry is idempotent |
| `/api/pairing/status` | receiver secret | `session_id` | Own state, expiry, device/client UUID, label and public comparison code |
| `/api/pairing/claim` | receiver secret | `session_id` | Once: `device_id`, `token_id`, `access_token`, `expires_at` |
| `/api/pairing/cancel` | receiver secret | `session_id` | Cancel pending/approved; for claimed session revoke its own device |

Device labels permit 1..48 ASCII letters/digits/spaces/dot/underscore/hyphen. The
server allocates device UUID separately from the phone's public registration UUID.
The comparison code is the full SHA-256 receiver-secret digest; possession of that
public digest cannot authenticate. Administrator compares session, device and code
with the phone through a trusted channel. Approval is not automatic and cannot
change invitation permissions. There is no anonymous CredentialManager adapter.

Session TTL: five minutes. Access TTL: one hour by default, administrator configurable
1..86400 seconds; claim cannot change it. At most eight active sessions and 128 retained
sessions; eight failed proofs per session; global 120 pre-auth service calls/minute
across all processes. Android polls PENDING every five seconds and then stops.
Failed proof counters commit before denial. In-memory caches are not used. Limits
provide bounded local operation, not public Internet DDoS protection. Known-session
budget exhaustion can deny a legitimate pairing; create a new invitation after review.

Trusted local CLI (loopback DB and development environment only):

```powershell
.venv\Scripts\python.exe scripts\pairing_admin.py invite --show-secret --permissions memory.read memory.write assistant.ask assistant.speech_plan
.venv\Scripts\python.exe scripts\pairing_admin.py pending
.venv\Scripts\python.exe scripts\pairing_admin.py approve --session-id SESSION_UUID --device-id DEVICE_UUID --verification-code RECEIVER_SHA256
.venv\Scripts\python.exe scripts\pairing_admin.py reject --session-id SESSION_UUID --device-id DEVICE_UUID
.venv\Scripts\python.exe scripts\pairing_admin.py devices
.venv\Scripts\python.exe scripts\pairing_admin.py revoke --device-id DEVICE_UUID
.venv\Scripts\python.exe scripts\pairing_admin.py prune-expired --confirm
```

Only `invite --show-secret` prints a one-time invitation. No CLI operation prints a
long-term access token. Approval requires all three public selectors/code. Pruning
removes expired invitations and expired terminal sessions with no live trusted token;
abandoned unclaimed devices are revoked, device/token audit records remain. It frees
the retention budget; no automatic maintenance occurs on main or during startup.

Transaction lock order: global pairing limiter, session, device, token. Trusted
credential writes keep the existing device/token order. Claim-vs-revoke linearizes:
if revoke wins claim fails; if claim wins the committed token becomes inactive when
device revoke commits. Concurrent claims yield exactly one credential. Interrupted
or ambiguous commit/delivery requires re-pair; no retry recovers a consumed bearer
secret. Cancelled/disconnected requests cancel their owned transaction and drain
the pool. Storage exceptions/parameters never enter public responses or diagnostics.

Android Server Connection validates a canonical origin (no userinfo/path/query/fragment).
Release requires HTTPS. Debug permits HTTP only on loopback for ADB; it also retains
the explicit LOCAL_DEV switch. Configure a SECURE local test at `http://127.0.0.1:PORT`
and use `adb reverse tcp:PORT tcp:PORT`; Host/listener ports must agree. APPLY SERVER
replaces the local connection record and requires a fresh pairing for the selected
origin. Before changing origin, unpair/revoke old access; if offline, use administrator
CLI to remove orphaned server access later.

The phone saves receiver state before registration and a claim-intent marker before
redeeming. It persists the complete encrypted credential before publishing the
nonblocking snapshot. Registration retries prove both secrets; once registered,
the encrypted invitation is removed and only the receiver proof remains. Interrupted
claim or failed credential save never unlocks private functions. On app restart,
the client checks its own session/token state before publishing credentials. Expiry
clears the snapshot for the legacy access-only format; v3 session renewal is described
in the separate protocol below. HTTP 401 / WS refusal uses common
RequestAuthentication callbacks and verifies revocation; it cannot switch to LOCAL_DEV.
Local unpair always removes the secret; server cancellation is best-effort and offline
removal may need explicit administrator revocation. Loss of Keystore key/ciphertext
requires pairing again. System clock error can deny access early; server time remains
authoritative for expiry and approval.

Storage is a single AES-GCM encrypted AtomicFile under noBackupFilesDir, not ordinary
SharedPreferences; explicit fd sync and read-after-write verify replacement before
publishing a credential. Ciphertext version is authenticated as AAD. Android backup is
disabled and that directory is excluded from automatic backup/transfer. See the
[Keystore key-generation contract](https://developer.android.com/reference/android/security/keystore/KeyGenParameterSpec)
and [Android backup exclusions](https://developer.android.com/identity/data/autobackup?hl=en).
FLAG_SECURE protects sensitive screens. JVM tests use injected crypto/storage ports;
actual AndroidKeyStore persistence was also tested on a physical SM-S938B (Android 16).
No device attestation, hardware-key guarantee or protection against a compromised
OS/administrator is asserted.

### Validation and manual installation

```powershell
$env:KULAI_RUN_POSTGRES_INTEGRATION='1'
backend\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_pairing_postgres.py -q
backend\.venv\Scripts\python.exe -m pytest backend/tests --ignore=backend/tests/integration -q
cd mobile/android
.\gradlew.bat testDebugUnitTest assembleDebug --rerun-tasks --console=plain
```

Integration mutations/migrations/restore go only to marker-owned temporary databases.
Physical smoke used an isolated owned DB, synthetic Memory/AI runtime and a random
loopback port: PENDING restart, receiver comparison, explicit approval, Paired restart,
authenticated MEMORY, administrator revoke and refusal with no extra private data
call, local unpair and restoration of LOCAL_DEV/ADB 8000. Device reboot and physical
voice NOTE/ASK/TTS with real models were not run in that pairing smoke. Backend
protocol/regression tests cover those existing contracts independently.

Main installation is NOT part of this change. After separate user authorization,
stop writers/clients/admin commands; strict doctor -> new full backup -> actual
restore verification -> immediate source/archive revalidation -> guarded upgrade
using `security_schema.py upgrade --confirm-main-security-schema --backup-output`
outside the repo -> strict doctor and security check -> restart SECURE. The same
procedure now accepts an absent schema or valid v1/v2 and installs through security v3.
Existing canonical/device/token/pairing/rate fingerprints must match; no destructive
downgrade is supported. Historical canonical-only/v1/v2 backups retain explicit absence markers.

The five-minute TTL governs registration, approval and redemption. A consumed
session retains a restricted receiver capability for its own status/revocation,
including an expired/revoked indication, until explicit administrator pruning.
It cannot issue/recover/refresh a token after consumption or invitation expiry.

Future VPS topology is Android/Desktop/AI -> HTTPS/WSS -> VPS -> private backend
connection. Pairing proofs, device identities and the application service are not
IP/USB dependent. The ingress policy must explicitly validate trusted TLS termination,
Host/origin and proxy identity before enabling a public route. This task does not
trust forwarded headers or deploy LAN, a tunnel, TLS, discovery, QR or MCP. PostgreSQL,
Memory and model endpoints remain private; GPU inference need not move to the VPS.

## Finite session renewal (TASK 4D.3A)

New pairings on verified security v3 return an access credential plus a separate
refresh credential. A one-hour access limit, 30-day refresh limit and fixed 90-day
session deadline apply; shorter administrator-approved access TTLs are retained.
The renewal operation cannot increase approved actions or select a project. Consumed
refresh history detects replay and revokes every access in that family. Repeated
parallel refresh requests may therefore revoke the session; Android coalesces them.

The sole additional pre-auth exception is exact native `POST /api/security/refresh`
in SECURE on the current validated local transport. All other HTTP/WS policies remain
unchanged. Access Bearer and refresh capabilities are incompatible formats/types.
Refresh never invokes AI or Memory. See [the full contract and threat model](SESSION_RENEWAL_PROTOCOL.md)
for ambiguity, persistence, bounds, physical smoke and separately authorized installation.
See [remote ingress architecture](REMOTE_ACCESS_ARCHITECTURE.md) before future VPS work.
