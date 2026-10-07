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

Backups can contain all user data and should be stored outside the repository.
The backup tool requires compatible PostgreSQL client tools (`pg_dump` and, for
the restore smoke, `pg_restore`, `createdb`, and `dropdb`). It never installs
them. The real repository integration tests are opt-in and accept only a
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
saves do not survive application restart. Streaming partial transcripts,
mobile clients, embeddings, semantic search, and RAG are outside this alpha.

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
screen, LAN transport, WSS, authentication, embeddings, RAG, partial STT, or
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
PostgreSQL dependency. This foundation does not index Memory, write vectors,
perform retrieval, or run RAG.

## Memory vector indexing

`MemoryIndexingService` accepts a detached canonical `Memory` and a neutral
embedding provider. `prepare(memory=...)` embeds exactly `Memory.content`,
validates the configured dimension, and returns a reusable `VectorUpsertRequest`.
The namespace is `kulai_memory.memories.v1`; the record ID is `str(Memory.id)`.
Metadata contains only source Memory ID, provider ID, exact response model tag,
and embedding dimension. Model digest is omitted until a runtime metadata
source is available. Memory content and its arbitrary metadata are not copied.

Close the Memory read session before preparing the vector. The host caller
`index_memory` in `kulai_memory.indexing_persistence` prepares first, then opens
a fresh short session, uses the reusable `PgVectorStore`, and commits through
the transaction context. On failure it rolls back and returns a safe
`MemoryIndexingError`. `save_prepared_memory_vector` can retry an already
prepared request without another embedding. A returned upsert result from the
application service alone does not imply commit. Provider close/dispose remains
the caller's responsibility; reuse one provider throughout a batch.

Repeated indexing updates the same `(namespace, record_id)` row and leaves the
canonical Memory unchanged. No existing voice/client flow automatically indexes
Memory. Guarded backfill and semantic retrieval are described below.

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
Memory UUID and a session factory. It deletes the canonical Memory first, then
uses reusable vector delete for namespace `kulai_memory.memories.v1` and record
ID `str(memory_id)`. Both adapters use the same PostgreSQL session and transaction.
The helper returns only after commit; failures before commit roll back both
changes. The neutral application service does not own commit or rollback.

Deletion is idempotent. The result contains `memory_id`, `memory_deleted` and
`vector_deleted_count`. A Memory without a vector can be deleted; an orphan
vector can be cleaned up; repeating a completed delete returns false/zero.
Other Memory IDs and vector namespaces are preserved. This is hard deletion
of the canonical row, including its stored ingestion identity.

Host indexing and backfill lock the canonical Memory row with `FOR UPDATE`
before upsert, inside the short write transaction. Embedding still runs before
opening that transaction. Both indexing and deletion acquire canonical-row
locks before touching vectors. An index that finishes first is subsequently
deleted; an index waiting for a completed delete fails safely without writing
a vector. A prepared request for a deleted Memory cannot recreate its vector.

Deletion currently has no UI, CLI or transport endpoint. Real deletion and
concurrency tests use owned temporary databases under
`KULAI_RUN_POSTGRES_INTEGRATION=1`; automatic validation never deletes main DB
Memory. No vendor or schema changes are required.

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
Output contains only identifiers, counts and status. This task runs main dry-run
only, and adds no automatic indexing to Desktop, WebSocket, or Android.

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
provider/model/dimension metadata fails the entire retrieval; invalid identities
and orphan vectors are controlled integrity errors. No hits are silently dropped.

Golden v1 contains 16 synthetic English Memories and 16 queries (8 English/8
Polish), with stable IDs. Dataset and thresholds were frozen before real tests:
macro Recall@1 >= 0.60, Recall@3 >= 0.80, Recall@5 >= 0.90; MRR is truncated at 5.
The opt-in tests use owned temporary DBs. Ordinary tests need neither Ollama nor
PostgreSQL. Run the retrieval integration with
`KULAI_RUN_POSTGRES_INTEGRATION=1`; add `KULAI_RUN_OLLAMA_INTEGRATION=1` for golden
BGE evaluation.

This baseline has no score cutoff, reranker, index freshness detection or
automatic voice indexing. Unindexed Memories are invisible to vector search,
and tied scores retain the store's order. No LLM, RAG, client UI or schema changes
are included.
