# KulAI Memory

KulAI Studio project `kulai_memory`.

## Reproducibility

- Source template: `blank`
- Current pinned KulAI commit: `720d0f26c75ba4af24d395f98f15a37e4bd5067e`
- KulAI monorepo submodule: `vendor/kulai_modules`

`kulai.project.json` and the submodule gitlink define the current pin. The
`aa5b4bc93d3b05cd84fcf681f54d848f74d630c5` value in
`.kulai/bootstrap-baseline.json` is the immutable project-generation baseline.
The same historical value in `.kulai/migrations.json` records the source used
to compose the reusable `kvectorstorepg_0001` migration. It changes only when
that reusable migration graph is deliberately recomposed.

## Direct modules

- `kulai-db`
- `kulai-fastapi-core`
- `kulai-fastapi-health`
- `kulai-provider-ollama`
- `kulai-provider-ollama-embeddings`
- `kulai-provider-whisper`
- `kulai-rag`
- `kulai-tasks`
- `kulai-vector-store-pgvector`

## Automatically required modules

- `kulai-logging`
- `kulai-config`
- `kulai-errors`
- `kulai-cache`
- `kulai-embeddings`
- `kulai-llm`
- `kulai-transcription`
- `kulai-vector-store`

## Runtime status

The first desktop alpha runs in a native PySide6 window and calls the
application core directly in process. It does not start FastAPI or Uvicorn.
The generated ASGI entry point remains available separately at
`kulai_memory.main:app`.

The application core includes provider-neutral Whisper transcription and
idempotent transcript ingestion into PostgreSQL Memory. Empty VAD-filtered
transcripts create no Memory; callers identify retries with a stable UUID.

## Bootstrap and local run

Use KulAI Studio's project detail page to create the isolated `.venv`, install
and verify the project, run generated tests, and create the initial local
commit. Copy `backend/.env.example` to `backend/.env` before local runtime.

The canonical local PostgreSQL commands are:

```text
.venv\Scripts\python.exe scripts\postgres.py up
.venv\Scripts\python.exe scripts\postgres.py status
.venv\Scripts\python.exe scripts\postgres.py down
```

The wrapper always passes `--env-file backend/.env` to Compose. `down` preserves
the named data volume. For this workflow, keep `DATABASE_URL` unset and use the
single `DB_*` configuration in `backend/.env`; ambient database environment
overrides are rejected. Compose provides PostgreSQL 18 with pgvector, matching
the canonical backup/restore major, and does not run schema migrations. Run
migrations explicitly, then verify the database:

```text
.venv\Scripts\python.exe scripts\migrate.py upgrade
.venv\Scripts\python.exe scripts\db_doctor.py
```

Create a full custom-format PostgreSQL backup and verify it in an automatically
owned temporary database with:

```text
.venv\Scripts\python.exe scripts\db_backup.py --output <backup.dump>
.venv\Scripts\python.exe scripts\db_restore_smoke.py <backup.dump>
.venv\Scripts\python.exe scripts\db_backup_restore_drill.py
```

Backup publication verifies that the source did not change during the dump.
Restore verification compares complete read-only snapshots: revision, canonical
Memory, vectors, and ingestion tombstones. Tombstones protect deleted ingestion
identities from replay; their count and SHA-256 must match even when Memory and
vectors match. The owned drill includes a production-deleted synthetic Memory.
Only counts/hashes are reported, without tombstone IDs or timestamps.

Backups can contain all user data and must be stored outside the repository.
The backup tool requires compatible PostgreSQL client tools (`pg_dump` and, for
the restore smoke, `pg_restore`, `createdb`, and `dropdb`). It never installs
them. Normal backup and restore require full read-only `db_doctor` PASS, so an
outdated schema is deliberately blocked. Before migrating a 0003 source to local
head `kulai_memory_0004`, use the explicit pre-migration mode:

```text
.venv\Scripts\python.exe scripts\db_backup.py --output <outside-repo-backup.dump> --pre-migration-from kulai_memory_0003
.venv\Scripts\python.exe scripts\db_restore_smoke.py <outside-repo-backup.dump> --pre-migration-from kulai_memory_0003
```

The source must match the requested revision and the local migration graph must
have one head. The 0003 -> 0004 profile accepts exactly `alembic.current`,
`schema.memories` and `constraint.memories_revision_positive` failures, requiring
all legacy columns to be correct and the lifecycle columns/constraint to be absent.
All other checks must PASS. The explicit legacy 0002 profile also requires the
pending tombstone table to be absent; restored 0002 represents tombstones as None.
The reviewed historical 0002 -> 0003 profile remains supported when that is the
local head. There is no generic doctor bypass or tolerance for partial schemas,
diagnostics errors, or unrelated failures.

The owned backup drill accepts the same explicit `--pre-migration-from` for its
read-only configured-source preflight; its synthetic source/restore DBs use head.
Its default mode still requires full doctor PASS.

Restore keeps the source revision, verifies every durable fingerprint and the
unchanged source, and removes only its owned temporary database. Keep the backup;
migration remains a separate manual action. Strict 0004 snapshots include content
revision and UTC archive timestamps; legacy 0002/0003 fingerprints keep their
original serialization. Real zero-row tombstone fingerprints are required from
0003 onward. Source targets must be local development/test databases; protected
and temporary names cannot be supplied through the CLI.

The real repository integration tests are opt-in and accept only a
loopback development/test database:

```text
$env:KULAI_RUN_POSTGRES_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest backend\tests\integration -q
```

After bootstrap, run `.venv` Python with `scripts/dev.py`; the server binds to
`127.0.0.1`. The data-safety contract is in `docs/DATA_SAFETY.md`.

## Desktop alpha

Install the optional desktop dependencies into the project environment:

```text
.venv\Scripts\python.exe -m pip install -e ".\backend[desktop]"
```

Keep the canonical Whisper configuration in `backend/.env`:

```text
KULAI_WHISPER_MODEL=large-v3
KULAI_WHISPER_DEVICE=cuda
KULAI_WHISPER_COMPUTE_TYPE=int8_float16
KULAI_WHISPER_VAD_FILTER=true
KULAI_CUDA_DLL_DIR=
```

`KULAI_CUDA_DLL_DIR` may point to the local directory containing the CUDA 12
runtime DLLs required by CTranslate2. The desktop and WebSocket server validate
the directory and add it only to their current process before loading Whisper.
They never change the system PATH and never fall back to CPU.

Start PostgreSQL, migrate explicitly, and confirm the schema before launching:

```text
.venv\Scripts\python.exe scripts\postgres.py up
.venv\Scripts\python.exe scripts\migrate.py upgrade
.venv\Scripts\python.exe scripts\db_doctor.py
.venv\Scripts\python.exe scripts\desktop.py
```

Manual microphone smoke checklist:

1. Select the intended microphone.
2. Click `NAGRAJ`.
3. Say a short Polish note.
4. Click `STOP`.
5. Confirm that the transcript appears.
6. Confirm the `Saved` status.
7. Confirm the Memory appears in the recent list.
8. Record a second note and confirm that the already-loaded model is reused.

The alpha records mono PCM16 at 16 kHz, limits one recording to 10 minutes,
and removes its temporary WAV after transcription. A failed database save can
be retried in the same process without recording or transcribing again. Pending
saves do not survive application restart. Memory indexing is automatic.

The **Ask Memory** panel accepts text questions directly in Desktop. Start
loopback Ollama with `bge-m3:567m-fp16` and `qwen3.5:9b` available; configure
`KULAI_LLM_MODEL=qwen3.5:9b` and the shared
`KULAI_OLLAMA_BASE_URL=http://127.0.0.1:11434`. Enter a question and click **ASK**.
For a spoken question, choose the microphone and click **ASK BY VOICE**, then
**STOP QUESTION**. The panel shows **Listening...** and **Transcribing question...**;
the recognized question appears in the query field, independently of the Voice
Note transcript. The same text ASK path handles the transcribed question. Empty
speech shows **No speech detected** and skips retrieval and Qwen. Voice Note and
voice question are explicit separate modes; the app does not infer your intent.
The panel shows **Searching memory...**, then **Generating answer...**, the
grounded answer, and validated sources (rank, Memory UUID and score). It does
not show source content, vector metadata or prompts. Qwen can answer only from
the supplied canonical Memories; Memory instructions are untrusted historical
data. Unsupported questions show **Not enough information** and
`Nie mam wystarczających informacji w pamięci.` with no sources. Empty retrieval
never calls Qwen. Questions perform no database writes, are not saved as Memory,
and do not refresh Recent Memory.

One Desktop controller owns one shared BGE client for automatic indexing and
question embeddings. Its long-lived RAG runtime borrows BGE and owns one reused
Qwen client; startup creates no Qwen generation request, so the first ASK can
cold-load the model. All model and database operations run on the existing
worker asyncio loop, outside the Qt UI thread. Recording, processing, pending
save/indexing and ASK are serialized; the controls prevent conflicting starts.
The retrieval session closes before Qwen generation. Closing the window cancels
an active question before closing Qwen, the shared BGE, recorder and engine;
it does not wait for the 180-second generation timeout. Voice Note and Ask by
Voice share the same lazy Whisper large-v3/cuda/int8_float16/VAD provider and
model. Question WAVs are private temporary files, removed after transcription
on success, empty speech, failure and cancellation. Closing during capture stops
the recorder and deletes its WAV without invoking RAG. Thread-backed Whisper
inference cannot be interrupted immediately: shutdown waits for its audio read
to finish before deleting the WAV and releasing resources. No audio is retained.
Voice questions allocate no ingestion identity, never become Memory and never
trigger indexing writes or refresh Recent Memory.

The local `scripts/ask_memory.py` CLI remains available for diagnostics. There
is no reviewed semantic score cutoff or reranker yet. Desktop has no TTS,
conversational multi-turn or partial STT; Android has no RAG UI, and WebSocket
has no voice RAG flow. The existing voice-note save/index/Recent Memory flow is
unchanged.

The Desktop **Memory Library** shows **Active** and **Archived** Memories in
created-at/UUID descending order, with a bounded limit of 100 rows. Select a row
to read its full content; the table shows a preview, canonical revision and
status. **REFRESH** reloads the current filter and Recent Memory. Library reads
perform no model requests and run on the same worker thread as other operations.

For an active Memory, **EDIT** opens its current content and saves with the
observed revision. A changed edit increments that revision and commits the
canonical content and removal of the old vector before the existing shared BGE
indexer embeds the new content. Identical content is **UNCHANGED**, with no
revision change or reindex request. A stale editor cannot overwrite a newer
revision: **Memory changed. Reload it before editing.** triggers a reload.

**ARCHIVE** asks for confirmation and reversibly removes an active Memory from
retrieval. It preserves canonical content/identity/revision, atomically removes
the vector, and creates no tombstone. **RESTORE** returns an archived Memory to
Active and indexes its latest revision. Archived Memories can be read and
restored; editing is available only for active Memories. Text ASK and Ask by
Voice see the new content after edit, exclude archived Memories and can retrieve
restored Memories.

If edit or restore commits but indexing fails, the Library displays the updated
canonical state and **semantic indexing needs retry**. **RETRY INDEXING** runs
bounded reconciliation (at most 100 missing indexes) using the same BGE, without
repeating the edit/restore or changing canonical content. A no-op edit does not
repair a missing vector; use this retry action. Library operations are serialized
with Voice Note, Text ASK, Ask by Voice and pending save/indexing retry. Closing
during indexing cancels the work; a committed change remains durable and startup
reconciliation can repair its missing index. Public errors do not include Memory
content. There is no hard-delete UI yet; archive is the reversible removal flow.
Voice/RAG functionality remains available, and TTS is still outside the Desktop.

## Local WebSocket voice-memory adapter

Install development dependencies and start the existing ASGI app locally:

```text
.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
.venv\Scripts\python.exe scripts\dev.py
```

`ws://127.0.0.1:8000/ws/memory` implements protocol v1. One connection accepts
one logical note: send `recording.start`, stream raw binary PCM signed 16-bit
little-endian audio at 16 kHz mono, then send `recording.stop`. The server emits
canonical `session.ready`, `transcript.final`, `memory.saving`, `memory.saved`,
and `error` events with one session ID and increasing sequence numbers. It
closes cleanly after a saved or empty note.

The largest binary frame is 256 KiB and total audio is limited to 19,200,000
bytes (10 minutes). After a recoverable `memory.save_failed`, `memory.retry`
reuses the transcript, ingestion ID, and session ID without running Whisper
again. There is no partial STT. This adapter remains localhost-only and has no
authentication until LAN/WSS pairing is added in TASK 4D.

## Android voice-memory alpha

The native Android alpha lives in `mobile/android`. It uses Kotlin, Jetpack
Compose, `AudioRecord`, and OkHttp. The debug build records voice-recognition
PCM16 at 16 kHz mono and streams bounded binary frames to the existing
WebSocket protocol v1. It keeps a private cache copy only while a note can be
retried, uses one stable ingestion UUID for every retry, and deletes the cache
after `memory.saved`, an empty transcript, or a nonrecoverable error.

Build and test the debug APK from the repository root:

```text
cd mobile\android
gradlew.bat testDebugUnitTest
gradlew.bat assembleDebug
```

The APK is written to:

```text
mobile\android\app\build\outputs\apk\debug\app-debug.apk
```

The debug client deliberately uses `ws://127.0.0.1:8000/ws/memory`. Cleartext
is enabled only in the debug manifest. The main/release manifest does not
enable it. Start the backend on loopback, attach a debug-authorized Android
device, install the APK, and configure ADB reverse:

```text
.venv\Scripts\python.exe scripts\dev.py
adb reverse tcp:8000 tcp:8000
adb reverse --list
adb install -r mobile\android\app\build\outputs\apk\debug\app-debug.apk
```

The backend must remain bound to `127.0.0.1`; TASK 4C does not expose it to the
LAN. On the phone, grant microphone permission, record a short Polish note,
stop, and confirm `Saved`. Then record 2–3 seconds of silence and confirm `No
speech detected` without a new Memory. Disconnect recovery uses `RETRY NOTE`
to replay private cached PCM with the same ingestion ID. A live recoverable
database failure uses `RETRY SAVE` without replaying audio or rerunning STT.

This alpha does not retain retry state across process death. It has no history
screen, LAN transport, WSS, authentication, RAG UI, partial STT, or
public-storage audio. LAN/WSS and authentication/pairing belong to TASK 4D.

## Local embedding foundation

The embedding host uses `kulai_embeddings.embed` and the reusable native Ollama
provider. Defaults are `KULAI_EMBEDDING_MODEL=bge-m3:567m-fp16` and
`KULAI_OLLAMA_BASE_URL=http://127.0.0.1:11434`; only loopback hosts are accepted.
The model must already be installed. No model is downloaded automatically.

Run the synthetic probe without reading Memory or opening PostgreSQL:

```text
.venv\Scripts\python.exe scripts\embedding_smoke.py
```

The probe uses the native model dimension, with no dimensions override,
truncation, padding, or normalization by the host. It requires an explicitly
configured `KULAI_VECTOR_DIMENSION` and compares it with the response. The
verified BGE-M3 output and current database schema both have dimension 1024.
Output contains only safe model/dimension metrics. Verify the actual schema
separately with `scripts/db_doctor.py`; the embedding probe does not inspect it.

Run the opt-in real Ollama test in PowerShell:

```powershell
$env:KULAI_RUN_OLLAMA_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest backend\tests\integration\test_embeddings.py -q
Remove-Item Env:KULAI_RUN_OLLAMA_INTEGRATION
```

The integration uses two synthetic requests on one provider/client, with no
PostgreSQL dependency. Memory indexing, retrieval and text RAG build on this
foundation through the separate services described below.

## Local text RAG over canonical Memory

`scripts/ask_memory.py` runs query → native BGE-M3 query embedding → existing
canonical semantic retrieval → bounded Memory context → local Qwen → grounded
answer with validated Memory citations. It requires local development settings,
loopback PostgreSQL/Ollama, `db_doctor` PASS at `kulai_memory_0004`, exact BGE
`bge-m3:567m-fp16` / native dimension 1024, and an installed LLM model:

```text
KULAI_LLM_MODEL=qwen3.5:9b
KULAI_OLLAMA_BASE_URL=http://127.0.0.1:11434
```

No model is downloaded by the application. Run locally in PowerShell:

```powershell
.venv\Scripts\python.exe scripts\ask_memory.py `
    --query "Gdzie mieszka mój zielony smok?" `
    --top-k 5
```

The CLI is read-only and requires no backup. Query must be nonblank and at most
10,000 characters; top-k defaults to 5 and accepts 1..20. Normal output is
`query=...`, `answer=...`, `sufficient_context=true|false`, and `citations=...`
(canonical Memory UUIDs). It shows neither context nor prompts by default.
`--show-context` explicitly displays Memory content for local debugging.

The application uses the neutral `kulai_llm.generate_structured` contract and
the reusable `kulai-provider-ollama` adapter. JSON Schema output is parsed and
strictly validated, with no output repair. A sufficient answer needs at least
one citation; unknown IDs fail safely. IDs must belong to the actual supplied
canonical evidence, and duplicates are removed in first-occurrence order.
The public `MemoryRagResult` contains the query, answer, sufficient-context
flag, citations with rank/score, and retrieval counts/budget/metric. It contains
no vectors, arbitrary Memory/vector metadata, context, or raw provider payload.

Memory is user-authored historical **untrusted data**. The stable system
instruction forbids executing commands inside Memory, disclosing system
instructions, inventing facts/citations, or filling gaps with general knowledge.
Evidence is a separately labeled JSON user message; the question is a separate
JSON user message. Each Memory has `memory_id`, `rank`, and canonical `content`.
JSON escaping keeps malicious delimiters inside the content value. Prompt
instructions mitigate injection; they do not formally prove model compliance.

Context includes whole Memories in retrieval order up to a global **12,000
character** limit, including JSON wrappers, IDs, ranks, and escaping. The
builder stops before the first block that does not fit and never joins partial
Memories. The reviewed `kulai-rag` API has no public standalone context builder
or token counter; its answer helper also performs a different chunk-metadata
retrieval. Therefore canonical Memory retrieval stays unchanged and the host
uses an explicit conservative character budget. Characters are not tokens;
this is not a tokenizer-based guarantee of context-window occupancy.

Zero hits (or no whole Memory fitting the budget) return
`Nie mam wystarczających informacji w pamięci.` with `sufficient_context=false`,
zero citations, and **zero LLM calls**. For unrelated evidence, Qwen must return
the insufficient-context flag; the application normalizes that answer to the
same canonical message. No reviewed semantic score cutoff or reranker exists
yet; both remain outside TASK 6A.1. Citation validation proves provenance, not
semantic entailment of every generated sentence.

One `MemoryRagRuntime` scope owns and reuses one BGE provider/client and one
Qwen provider/client, closing both on success, errors and cancellation. Query
embedding runs without a DB checkout. Existing retrieval uses one short
REPEATABLE READ, READ ONLY snapshot to resolve active canonical identities and
exact revisions, then rolls back and closes its session **before Qwen**.
The LLM has an explicit 180-second generation/HTTP timeout and the CLI a
300-second overall deadline. Qwen uses non-streaming schema output with
thinking disabled; no Whisper is loaded by the CLI. Desktop uses this same
canonical RAG through its text ASK and ASK BY VOICE controls and borrows its
shared BGE provider. WebSocket and Android RAG UX are not integrated.

The real RAG integration uses **only owned temporary databases and synthetic
Memories**, never real main content:

```powershell
$env:KULAI_RUN_POSTGRES_INTEGRATION = "1"
$env:KULAI_RUN_OLLAMA_INTEGRATION = "1"
$env:KULAI_RUN_LLM_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest backend\tests\integration\test_memory_rag_postgres.py -q -s
```

It checks Polish grounding, unsupported questions, prompt injection, exact
citations, complete snapshot equality, client reuse/closure, and zero DB
checkout during Qwen. Synthetic PostgreSQL tests additionally check latest
edits, archive exclusion, and stale-vector rejection before generation.

## Memory vector indexing

`MemoryIndexingService` accepts a detached canonical `Memory` and a neutral
embedding provider. `prepare(memory=...)` embeds exactly `Memory.content`,
validates the configured dimension, and returns a reusable `VectorUpsertRequest`.
The namespace is `kulai_memory.memories.v1`; the record ID is `str(Memory.id)`.
Metadata contains only source Memory ID, provider ID, exact response model tag,
embedding dimension, and canonical content revision. Model digest is omitted until a runtime metadata
source is available. Memory content and its arbitrary metadata are not copied.

Close the Memory read session before preparing the vector. The host caller
`index_memory` in `kulai_memory.indexing_persistence` prepares first, then opens
a fresh short session, uses the reusable `PgVectorStore`, and commits through
the transaction context. On failure it rolls back and returns a safe
`MemoryIndexingError`. `save_prepared_memory_vector` can retry an already
prepared request without another embedding. A returned upsert result from the
application service alone does not imply commit. Provider close/dispose remains
the caller's responsibility; reuse one provider throughout a batch.

Explicit reindex updates the same `(namespace, record_id)` row and leaves the
canonical Memory unchanged. Desktop and WebSocket voice saves now automatically
ensure a compatible vector after the canonical Memory transaction commits and
closes. `CREATED` indexes the new Memory; `DUPLICATE` repairs a missing vector and
skips a compatible existing vector with zero embedding requests or writes.
Empty transcripts still create neither Memory nor vector.

Automatic indexing requires `source_memory_id`, provider `ollama`, model
`bge-m3:567m-fp16`, dimension `1024`, and the current canonical `revision` at the exact namespace/record identity.
An incompatible vector is reported and never automatically overwritten. A final
metadata check under the canonical `FOR UPDATE` lock prevents concurrent workers
from overwriting a compatible vector and prevents stale embeddings from reviving
a deleted, archived, or edited Memory. Query retrieval also requires matching
`source_memory_id` and canonical `revision`.

Each runtime owns one reusable embedding provider/client and closes it during
shutdown or startup failure. Embedding runs without a checked-out Memory session;
vector upsert and caller-owned commit use a separate short transaction. Each
embedding has a 120-second deadline. Canonical Memory remains durable if indexing
fails. WebSocket emits recoverable `memory.index_failed` and retains the existing
`memory.retry` flow; `memory.saved` is emitted after indexing succeeds. Desktop
shows `INDEXING_FAILED`, preserves the Memory ID, refreshes Recent Memory and
offers retry. Android maps the same error to its existing retry flow. Retries
preserve ingestion ID/transcript and do not rerun Whisper.

Canonical Memory without a vector is the durable crash-recovery signal. Startup
reconciliation repairs at most 100 missing vectors, ordered by `created_at ASC,
id ASC`, after the database doctor passes. It awaits that bounded batch before
readiness on the runtime worker loop, never the Qt UI thread. Compatible vectors
are skipped; incompatible vectors are counted separately. The first indexing
failure stops the batch and leaves the runtime available with a safe degraded
report (Desktop status / server warning and `indexing_report`). A later bounded
`reconcile_missing_indexes()` call, restart or duplicate note retry can repair
the gap. Remaining missing vectors also mark the report degraded. There is no
periodic polling, outbox migration or automatic reindex of incompatible vectors.
Guarded manual backfill and semantic retrieval are described below.

Integration tests create, migrate, and remove owned temporary databases only:

```powershell
$env:KULAI_RUN_POSTGRES_INTEGRATION = "1"
# Add this opt-in for real BGE-M3 + real pgvector rather than synthetic embeddings:
$env:KULAI_RUN_OLLAMA_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest backend\tests\integration\test_memory_indexing_postgres.py -q
Remove-Item Env:KULAI_RUN_POSTGRES_INTEGRATION
Remove-Item Env:KULAI_RUN_OLLAMA_INTEGRATION
```

The tests prove durable upsert/reindex, commit failure rollback, Memory safety,
provider/client reuse, and embedding without an open database transaction.

## Atomic Memory deletion

The backend helper `kulai_memory.deletion_persistence.delete_memory` accepts a
Memory UUID and a session factory. It retires ingestion identity and deletes the canonical Memory, then
uses reusable vector delete for namespace `kulai_memory.memories.v1` and record
ID `str(memory_id)`. Both adapters use the same PostgreSQL session and transaction.
The helper returns only after commit; failures before commit roll back the tombstone and both
changes. The neutral application service does not own commit or rollback.

Deletion is idempotent. The result contains `memory_id`, `memory_deleted` and
`vector_deleted_count`. A Memory without a vector can be deleted; an orphan
vector can be cleaned up; repeating a completed delete returns false/zero.
Other Memory IDs and vector namespaces are preserved. This is hard deletion
of the canonical row. `memory_ingestion_tombstones` retains only `ingestion_id`,
`memory_id` and `deleted_at`, with no content, metadata, session or vector data,
no foreign key, and no expiry. Repeated deletion preserves the first timestamp.

Creation and deletion serialize across sessions/processes through PostgreSQL
transaction advisory locks. The signed 64-bit key is the first eight bytes of
SHA-256 over `kulai_memory.ingestion.v1\0` followed by the ingestion UUID bytes.
A collision only serializes unrelated identities; tombstone checks use the full
UUID. The lock order is ingestion advisory lock, canonical row, then vector.
Indexing only locks canonical row then vector and never takes an advisory lock.
Checks use the host's PostgreSQL READ COMMITTED transactions after acquiring the
lock, so a waiting replay sees the committed tombstone before any INSERT.

A retry of a retired ingestion UUID raises `MemoryIngestionRetiredError`, even
with different content. Fresh ingestion UUIDs still work. WebSocket returns
`memory.ingestion_retired` with `recoverable=false` and closes with code 1008;
desktop clears pending save and offers no retry for that deleted note.

Host indexing and backfill lock the canonical Memory row with `FOR UPDATE`
before upsert, inside the short write transaction. Embedding still runs before
opening that transaction. Both indexing and deletion acquire canonical-row
locks before touching vectors. An index that finishes first is subsequently
deleted; an index waiting for a completed delete fails safely without writing
a vector. A prepared request for a deleted Memory cannot recreate its vector.

Deletion currently has no UI, CLI or transport endpoint. Real deletion and
concurrency tests use owned temporary databases under
`KULAI_RUN_POSTGRES_INTEGRATION=1`; automatic validation never deletes main DB
Memory. Host revision `kulai_memory_0003` adds the technical tombstone table;
the reviewed vendor pin is unchanged. Runtime requires the current schema head
`kulai_memory_0004`. A database still on 0003 needs an explicitly reviewed manual
upgrade after pre-migration backup and restore verification. Strict doctor and
startup validation reject outdated schemas; runtime never upgrades or stamps
the database automatically.

## Memory edit, archive and restore

Memory has a positive `revision` (initially 1) and nullable UTC `archived_at`.
The neutral lifecycle service changes canonical Memory and uses reusable vector
delete in one caller-owned PostgreSQL transaction. Mutation lock order is ingestion
advisory lock -> canonical row -> vector, matching hard deletion. IDs, ingestion
identity, creation time and original source/session metadata are preserved.

EDIT requires `expected_revision`; stale editors receive `memory.revision_conflict`.
Changed content increments revision and removes the previous vector atomically.
An identical-content edit leaves revision/vector unchanged. After canonical COMMIT
and session close, automatic indexing embeds the exact new content and writes
metadata `revision` matching Memory. An indexing failure preserves the committed
edit and reports INDEXING_FAILED; startup reconciliation or guarded missing-only
backfill repairs it. Old voice retries with the original content receive a terminal
idempotency conflict rather than overwriting edited content or looping retry.

ARCHIVE sets `archived_at` and deletes the exact vector atomically. It retains the
Memory and creates no tombstone. Repeated archive preserves its first timestamp.
Archived Memories are excluded from Recent Memory, retrieval, automatic recovery
and both backfill modes. A voice replay cannot unarchive them; it receives terminal
`memory.archived`. EDIT requires RESTORE first.

RESTORE clears `archived_at`, commits, then ensures indexing of the existing
content/revision. A repeated restore can repair a missing vector. Prepared vectors
are checked against active canonical revision under FOR UPDATE in every host write
path, including manual reindex. Stale/archived vectors are never written; unexpected
archived or revision-incompatible search hits fail safely. Desktop Memory Library
provides EDIT, ARCHIVE, and RESTORE controls. There is no public HTTP/WebSocket
lifecycle endpoint yet, and hard DELETE has no Desktop UI.

`scripts/manage_memory.py` provides `show`, `edit`, `archive`, and `restore`:

```text
.venv\Scripts\python.exe scripts\manage_memory.py show --memory-id <UUID>
.venv\Scripts\python.exe scripts\manage_memory.py edit --memory-id <UUID> --expected-revision <N> --content-file <UTF8-file> --dry-run
.venv\Scripts\python.exe scripts\manage_memory.py archive --memory-id <UUID> --dry-run
.venv\Scripts\python.exe scripts\manage_memory.py restore --memory-id <UUID> --dry-run
```

Mutations default to dry-run with zero embedding/backup/write calls. Execution is
an explicit real-data operation requiring `--execute`, `--confirm-main-memory-write`
and a new `--backup-output` outside the repository. Doctor, local environment and
loopback configuration guards must pass; backup plus owned restore verification
must complete before any mutation. EDIT/RESTORE reuse one BGE client for synthetic
preflight and indexing; ARCHIVE works without Ollama. Backups are retained. A saved
canonical change with failed indexing returns nonzero and explicitly says retry
is available; use the existing guarded missing-only backfill or startup recovery.
The UTF-8 content file remains owned by the user. Only explicit `show` displays
canonical content; mutation output and error messages omit content/vector values.

Migration 0004 marks only exact compatible legacy vectors lacking `revision` as
revision 1, without re-embedding. Incompatible records and other namespaces remain
unchanged. No edit history is stored. Schema migration and real main lifecycle
writes require separate review; automated tests use owned DBs only.

## Guarded Memory backfill

`scripts/index_memories.py` defaults to **dry-run** on the configured database.
It performs read-only diagnostics and selection, without creating a provider,
contacting Ollama, making a backup, or writing vectors:

```text
.venv\Scripts\python.exe scripts\index_memories.py --dry-run --missing-only
.venv\Scripts\python.exe scripts\index_memories.py --dry-run --reindex --limit 100
```

Selection is deterministic: `created_at ASC, id ASC`. `--limit` accepts 1–1000
(default 100); `--memory-id UUID` restricts selection. Default `--missing-only`
excludes any existing exact namespace/record identity. Incompatible existing
provider/model metadata is reported and needs explicit `--reindex`; it is never
silently treated as compatible or automatically replaced.

**Execute writes vectors for real user Memory. Run it only after separately
approving the dry-run selection and first main backfill.** It requires both
`--confirm-main-vector-write` and `--backup-output PATH`, with a new file in an
existing directory outside this repository. There is no force/overwrite option.
It permits only local development environments and loopback DB/Ollama, requires
doctor PASS and exact BGE-M3/native/config/schema dimension 1024, and performs a
synthetic preflight before any Memory embedding. One provider is reused for the
preflight and entire batch.

Before any write, a custom-format backup is created and verified by SHA-256 and
restore into an owned temporary database. Restore/source fingerprints must
match. The backup is retained after success or failure. No automatic migration
or database reset is performed.

Each Memory read session closes before embedding; each vector uses a new short
write transaction and commit. On a partial failure, earlier commits remain,
the current failed write rolls back, and rerunning missing-only resumes safely.
Memory fingerprints are checked before writes, between items, and at completion.
Concurrent Memory changes stop the operation with a safe error; keep voice saves
idle during a future approved backfill if an unchanged fingerprint is required.
Output contains only identifiers, counts and status. Manual backfill remains a
separate guarded operation; runtime automatic indexing uses ensure-indexed and
never performs implicit reindex.

## Semantic Memory retrieval

The retrieval foundation embeds an unchanged query with native
`bge-m3:567m-fp16` (1024), then uses reusable pgvector search scoped to
`kulai_memory.memories.v1`. Cosine scores are `1 - cosine_distance`;
higher scores indicate better matches. Each vector record ID is resolved to
canonical content in `memories`, preserving the store's ranking. Results do not
contain embedding vectors or raw vector metadata.

The reviewed reusable root fix is pinned at `720d0f2`. It normalizes query
vectors to lists at the pgvector driver boundary while preserving the public
tuple contract. Real pgvector retrieval passes with `pgvector 0.5.0`. On frozen
golden v1, Recall@1, Recall@3, Recall@5 and MRR@5 are all 1.00, including both
English and Polish queries. The local semantic CLI also passes against main DB;
Memory and vector fingerprints remain unchanged. These metrics describe the
controlled synthetic dataset, not general retrieval quality.

The explicit local CLI is:

```text
.venv\Scripts\python.exe scripts\search_memories.py --query "aplikacja mobilna KulAI Memory" --top-k 5
```

It checks doctor/model/dimension and loopback DB/Ollama, owns one provider, and
closes it on failure or success. Queries must be non-blank strings of at most
10,000 characters; top-k accepts integers 1–20 (default 5). Query embedding runs
before opening a new repeatable-read, read-only session. Search and canonical
lookup share that snapshot; it is rolled back and closed without commit.

The CLI intentionally displays canonical Memory content for the user. Automated
main smoke and reports show only counts, rank, UUID and score. Incompatible
provider/model/dimension/revision metadata fails the entire retrieval; invalid identities
and orphan vectors are controlled integrity errors. No hits are silently dropped.

Golden v1 contains 16 synthetic English Memories and 16 queries (8 English/8
Polish), with stable IDs. Dataset and thresholds were frozen before real tests:
macro Recall@1 >= 0.60, Recall@3 >= 0.80, Recall@5 >= 0.90; MRR is truncated at 5.
The opt-in tests use owned temporary DBs. Ordinary tests need neither Ollama nor
PostgreSQL. Run the retrieval integration with
`KULAI_RUN_POSTGRES_INTEGRATION=1`; add `KULAI_RUN_OLLAMA_INTEGRATION=1` for golden
BGE evaluation.

This baseline has no score cutoff or reranker; revision checks enforce content freshness.
Missing indexes awaiting repair are invisible to vector search,
and tied scores retain the store's order. Retrieval adds no LLM, RAG or client UI.
