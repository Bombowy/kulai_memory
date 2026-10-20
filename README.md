# KulAI Memory

KulAI Studio project `kulai_memory`.

Current priority: the [simple voice RAG MVP](docs/MVP_SCOPE.md), with durable
Memories, NOTE / ASK / MEMORY and optional TTS. Fact Intelligence (5A.1–5A.6)
development is paused; its demos remain separate from the normal runtime.

V2.2 (branch `feature/kulai-memory-v2-conversations`) adds the explicit canonical
migration `kulai_memory_0005` after `0004`, containing only the three conversation
tables from V2.1. The doctor and existing full PostgreSQL backup/restore include
conversation metadata, request identities and ordered messages. Security stays
at `kulai_security_0003`; Memories and pgvector are unchanged. Nothing migrates at
application startup. This branch is not deployed: the running V1 database stays
at `0004` until the controlled V2 cutover. Before that migration, the existing
backup/restore commands require explicit `--pre-migration-from kulai_memory_0004`;
strict mode requires `0005`. Restore verification uses marker-owned temporary
databases and compares all conversation fingerprints, including empty tables.

TASK 4D.1 adds a neutral security core and shared HTTP/WebSocket enforcement.
The ASGI app defaults to `SECURE` and uses a managed PostgreSQL credential reader
only after strict schema verification. TASK 4D.2A adds an independent security
migration graph; canonical head stays `kulai_memory_0004`. `scripts/dev.py` preserves
loopback `LOCAL_DEV` and ADB reverse. Main security migration requires explicit
review and verified backup/restore. TASK 4D.2B adds local administrator-approved
Android pairing, Keystore credential storage and security head `kulai_security_0002`.
TASK 4D.3A adds rotating refresh credentials, finite persistent sessions and independent
security head `kulai_security_0003`, with automatic Android renewal and replay revocation.
See [session renewal](docs/SESSION_RENEWAL_PROTOCOL.md) and the
[remote architecture and implemented mTLS ingress](docs/REMOTE_ACCESS_ARCHITECTURE.md).
TASK 4D.3B.1 implements opt-in loopback TLS/mTLS with same-process trusted ASGI,
HTTPS/WSS, strict gateway SAN/Host checks and SECURE/schema deployment guards.
VPS/Nginx/WireGuard deployment remains future work; no main migration is installed here.
See the [security contract](docs/LOCAL_ASSISTANT_TRANSPORT.md#persistent-postgresql-security-store-task-4d2a).

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
is no reviewed semantic score cutoff or reranker yet. Desktop has no
conversational multi-turn or partial STT. Android provides text/voice ASK through
the local server transport described below. The existing
voice-note save/index/Recent Memory flow is unchanged.

Desktop supports optional **local Polish / English speech** on Windows using installed
Microsoft **Desktop** voices through `System.Speech`, with the existing
`sounddevice` audio output. No cloud inference, additional model download or
voice binaries in the repo are required. This host has **Microsoft Paulina
Desktop** (`pl-PL`) and **Microsoft Zira Desktop** (`en-US`). Voices are Windows
components under their installed Microsoft licensing terms; no weights are
redistributed. Other configured voices must be installed, enabled Microsoft
Desktop voices with the matching language. See the
[System.Speech API](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer?view=netframework-4.8.1).

Set these values in `backend/.env` to enable speech:

```dotenv
KULAI_TTS_ENABLED=true
KULAI_TTS_PL_VOICE=Microsoft Paulina Desktop
KULAI_TTS_EN_VOICE=Microsoft Zira Desktop
```

The example defaults to disabled. Missing settings or unavailable voices show
**TTS unavailable.** while Voice Note, RAG and Library remain usable. Startup
checks installed voices without synthesizing speech or requesting Qwen.
One long-lived local TTS process owns one synthesizer and switches its installed
PL/EN voices; it does not reload a model for each segment. Each synthesis selects
the exact configured voice and verifies the active voice before speaking; Python
also rejects returned voice IDs that do not match the segment's PL/EN routing.

**Speak answers automatically** is checked by default and applies to both text
**ASK** and **ASK BY VOICE**. Each successful or insufficient-context result
renders its answer and validated sources first, then starts speech exactly once
if TTS is available. Uncheck it to use **SPEAK** manually for either kind of answer.
UUIDs, citations, scores and source content are never sent to TTS. A separate
speech status shows **Preparing speech...**, **Synthesizing speech...**,
**Speaking...** and **Speech finished**. A failure shows **Could not speak answer.**
without changing the answer, sources or RAG status.

The existing Qwen client partitions only the final answer into exact fragments
labelled `pl` or `en`, treating that text as untrusted data. The neutral speech
contract accepts 1–64 nonempty segments and at most 6,000 characters. Their
concatenation must equal the answer character for character, including whitespace
and punctuation. Translation, rewriting, missing or duplicated characters fail
safely; the app does not repair them or fall back to reading everything in PL.
Adjacent fragments with the same language are merged after validation. A fixed
insufficient-context answer uses one PL segment without another Qwen request.
The planner partitions phrases within sentences as well as complete sentences;
full English clauses use EN even after a Polish opening. Names/acronyms alone
may stay with the surrounding phrase. Only Polish and English are supported;
other languages require a separate design. Labels come from Qwen; exact-text
validation cannot prove classification for every possible answer, so pronunciation
still needs manual review.

Each PL fragment uses the configured Polish voice, each EN fragment the English
voice. PCM16 WAVs play in order through separate private output streams, preserving
each file's sample rate/channels. The app never concatenates WAV headers or plays
two answers together. Audio lives only in unique private temporary files outside
the repo. **STOP AUDIO** cancels planning/synthesis or stops playback and cleans
the artifacts; it preserves text and lets you **SPEAK** again. New ASK, voice
question or Voice Note waits for previous speech to stop and drain completely
before starting. The UI submits one new operation; that controller operation
owns the stop-and-drain sequence, avoiding competing STOP/request futures.

Planning, synthesis, file IO and playback run on the existing worker; they perform
no database writes and hold no DB checkout. Planning/synthesis use the operation
lock. During playback, a new question/Voice Note can stop it; Library operations
remain disabled until speech ends. Limits are 180 seconds for planning, 90 seconds
per synthesis, 600 seconds for the whole speech operation, 16 MiB per WAV and
64 MiB total audio. Cancellation drains native synthesis before cleanup, bounded
by its synthesis timeout; shutdown closes TTS once and then existing providers.
Completed playback, STOP, failure, cancellation, replacement and shutdown remove
owned audio. There is no permanent audio cache or TTS history.

Desktop speech uses System.Speech; Android also supports local Polish / English
speech using installed offline voices and canonical server speech planning.
WebSocket/audio TTS transport, multi-turn conversation, streaming partial STT/TTS
and a reviewed score cutoff/reranker are not implemented.
`KULAI_RUN_TTS_INTEGRATION=1` enables real installed-voice tests with synthetic
PL/EN/mixed text. Automated tests validate PCM/WAV and cleanup through a fake
audio-device boundary; listening quality and the physical output device are
checked manually after review.

For a local synthetic mixed-answer listening test without Memory or PostgreSQL:

```powershell
.\.venv\Scripts\python.exe scripts\tts_smoke.py --text "To jest część po polsku. This is the English part. I znowu po polsku."
.\.venv\Scripts\python.exe scripts\tts_smoke.py --text "This starts in English. Potem przechodzimy na polski. English again."
```

This uses the production planner, local voices and playback, prints only segment
count/language/character count/voice, and removes every temporary audio file.
`--no-play` runs synthesis/WAV validation without playback. It requires the same
TTS configuration and local Qwen model as Desktop; it never downloads voices.

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
content. Archive is the reversible removal flow; permanent DELETE is also available
for both Active and Archived Memories.
Voice/RAG functionality remains available alongside optional Desktop speech.

Desktop **DELETE** shows the selected UUID, revision and Active/Archived status
without displaying content. Cancel is the default; type exactly `DELETE` to enable
the destructive action. Choose a **new `.dump` path outside the repository**, with
an existing parent and no symlink/junction traversal. Existing files are rejected.

Every delete requires strict **db_doctor PASS**, a full PostgreSQL custom-format
backup with size/SHA-256, and an **actual restore into an owned temporary database**.
The restored complete Memory/vector/tombstone snapshot must match the source;
source changes or verification failures prevent deletion. The dump is retained
after success and after failure (a failed dump may be incomplete and unverified).
After verified backup creation returns, Desktop rechecks the complete source
snapshot and the retained regular file's size/SHA-256 immediately before delete.
Any change, including archive/restore or another Memory, blocks deletion; pending
cancellation also prevents starting the delete transaction.
Recovery requires the retained verified backup; there is no Undo or automatic
restore into the source. Keep the backup in a safe location.

Only then does the existing atomic delete run with `expected_revision`, checked
under the canonical row lock in the same transaction. A stale selection cannot
delete a newer revision: **Memory changed. Reload it before deleting.** Deletion
removes canonical Memory and its exact vector and writes an ingestion tombstone;
the retired ingestion identity cannot replay, even with different content. Library
and Recent refresh after success. DELETE makes no Whisper/BGE/Qwen requests.
Backup/subprocess/DB work runs on the existing worker, with conflicting actions
disabled. Closing drains backup/restore work without starting delete; cancellation
before commit rolls back, while an already committed deletion remains durable.

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
again. There is no partial STT. This adapter remains localhost-only and uses the
shared HTTP/WS authentication policy described above.

## Local server history and Memory assistant

The same local server now provides bounded read-only `GET /api/memories` history,
`GET /api/memories/{id}` canonical detail, `POST /api/assistant/ask` text RAG and
one voice question per `/ws/assistant` connection. History supports Active/Archived
and descending timestamp/UUID keyset cursors; detail exposes full content without
private ingestion/session/vector metadata. Assistant responses contain grounded
answer/status and validated citations, with the canonical insufficient-context
answer when evidence is missing. Questions and answers are never saved as Memory.

All transports reuse one server Whisper, one shared BGE (indexing + retrieval),
one Qwen and one DB pool. One AI lane serializes heavy inference; DB sessions
close before model work, and history can run while inference is busy. Voice
questions use private temporary WAVs that are cleaned after safe STT drain.
Disconnects cancel RAG promptly. Startup runs strict doctor and bounded indexing
reconciliation without Qwen generation; owned providers close once on shutdown.

See [the transport contract](docs/LOCAL_ASSISTANT_TRANSPORT.md) for complete
HTTP/WS schemas, cursor rules, events, audio limits and safe errors. `/ws/memory`
v1 and current Android Voice Note are unchanged. The new transport has no TTS,
authentication or LAN/WSS. Android uses these localhost APIs through ADB reverse
for history, detail and text/voice ASK. Desktop continues to run in process.
`POST /api/assistant/speech-plan` returns an exact PL/EN segment plan for a final
answer using the same server Qwen and AI lane, with no DB work or BGE/Whisper.
It returns no synthesized audio; playback belongs to the local client.

## Android NOTE / ASK / MEMORY alpha

The native Android alpha lives in `mobile/android`. It uses Kotlin, Jetpack
Compose, `AudioRecord`, and OkHttp. The debug build records voice-recognition
PCM16 at 16 kHz mono and streams bounded binary frames to the existing
WebSocket protocol v1. It keeps a private cache copy only while a note can be
retried, uses one stable ingestion UUID for every retry, and deletes the cache
after `memory.saved`, an empty transcript, or a nonrecoverable error.

**NOTE**, **ASK** and **MEMORY** keep independent in-memory state across tab
switches. ASK supports text queries and ephemeral **ASK BY VOICE** questions,
showing transcript, grounded answer and validated citations. Insufficient context
has no sources; silence skips RAG. Questions/answers are never saved as Memory,
and voice questions have no retry cache or ingestion UUID. A shared capture gate
prevents simultaneous Note/Question capture and waits for native capture drain.
Text ASK, history and detail remain usable without microphone permission.

Android ASK offers **SPEAK**, **STOP AUDIO** and **Speak answers automatically**
(default on for Text ASK and ASK BY VOICE). It requests canonical speech plans
using only the final answer and validates exact text and PL/EN labels. One local
Android TextToSpeech instance selects deterministic installed offline `pl-PL`
and `en-US` voices, verifies voice ID before each utterance and awaits ordered
playback. Missing either voice degrades to **TTS unavailable** without affecting
NOTE/ASK/MEMORY. Speech failure preserves the answer/citations. New questions
or Note capture stop previous speech; playback can continue across tab switches.
No synthesized audio is stored, and TTS works without microphone permission.

MEMORY provides Active/Archived history, timestamp/UUID keyset pagination,
**LOAD MORE**, **REFRESH**, full canonical detail and **BACK**. Successful Note
save marks history stale for its next visit. HTTP uses OkHttp cancellation and
explicit UTF-8 JSON; HTTP and assistant WS use strict typed projections, shared
answer validation and stale-response protection. See
[Android documentation](mobile/android/README.md) for the controls and smoke plan.

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

Debug uses `http://127.0.0.1:8000` for history/detail/text ASK,
`ws://127.0.0.1:8000/ws/assistant` for voice ASK, and the unchanged
`ws://127.0.0.1:8000/ws/memory` for Voice Note. Cleartext
is enabled only in the debug manifest. The main/release manifest does not
enable it. Start the backend on loopback, attach a debug-authorized Android
device, install the APK, and configure ADB reverse:

```text
.venv\Scripts\python.exe scripts\dev.py
adb reverse tcp:8000 tcp:8000
adb reverse --list
adb install -r mobile\android\app\build\outputs\apk\debug\app-debug.apk
```

The backend must remain bound to `127.0.0.1`; this alpha does not expose it to the
LAN. On the phone, grant microphone permission, record a short Polish note,
stop, and confirm `Saved`. Then record 2–3 seconds of silence and confirm `No
speech detected` without a new Memory. Disconnect recovery uses `RETRY NOTE`
to replay private cached PCM with the same ingestion ID. A live recoverable
database failure uses `RETRY SAVE` without replaying audio or rerunning STT.

This alpha does not retain retry state across process death. History/assistant
state is in-memory. There is no arbitrary-language TTS, lifecycle mutation UI, LAN transport,
WSS, multi-turn, partial STT, or public-storage audio. Authentication and local
pairing are implemented in TASK 4D; public ingress remains a separate task.

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
shared BGE provider. The local server also exposes canonical text/voice RAG;
Android exposes it through the ASK tab without storing questions or answers.

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

## Memory intelligence preview (TASK 5A.1)

The opt-in local CLI extracts atomic proposals from UTF-8 files, checks exact
source evidence (native JSON Schema restricts quote choices to actual source
sentences or the whole source), independently verifies proposals with Qwen3.5:9b,
and suggests duplicates, related information, possible conflicts and replacements.
It always runs in dry-run mode. It creates no Memories, vectors or fact records,
and is not invoked by NOTE, ASK, voice, TTS or server startup.

```powershell
.venv\Scripts\python.exe scripts\memory_intelligence_preview.py `
    --input C:\private\new-note.txt --compare-file C:\private\previous-note.txt --dry-run
```

Normal output contains counts, candidate IDs, information types, relation labels,
similarity hints and review status. Add `--show-candidates` to explicitly display
private text, exact quotes, Unicode character offsets (end exclusive), source
hashes, Memory ID/revision when applicable, temporal context and relation rationale.
Keep private input files outside the repository. The CLI does not save outputs.
Input is limited to 12,000 characters per source and 20 proposals per extraction;
oversized sources fail explicitly instead of being truncated.

`--compare-file` can be repeated. Without `--compare-main`, there is no database
configuration, doctor, engine or session. `--compare-main` explicitly enables local
administrative read-only BGE lookup against already indexed active canonical
Memories; it requires strict canonical doctor PASS at `kulai_memory_0004` and
compatible 1024-dimensional vectors. Missing/stale vectors are never repaired.
An absent security schema is reported as `ABSENT_SECURE_DOCTOR_FAIL`; this local
administrator tool does not change the server's SECURE startup requirements.
No schema migration is added by this feature.

First-person authors are scoped to each source by default. `--speaker-id local-user`
explicitly shares the narrator's identity across all supplied/read sources, while
named and quoted people keep their own attribution. Different project names and
person identities conservatively block equivalence/conflict suggestions. Entity
aliases are not resolved automatically.
Possible replacements additionally require an explicit PL/EN replacement phrase
in the proposed replacement's evidence (for example `zamiast` / `instead of`).
Different dates alone are downgraded to related information, with
`validation_notes=["replacement_not_explicit"]`. This lexical guard is conservative
and can miss other ways of expressing a change; it is not a semantic proof.

Plans, preferences and hypotheses remain distinct from facts. Negation and explicit
time phrases are retained; source timestamps are not inferred event times. All
returned candidates and relations have `review_status=requires_review`. Exact
quotes prove source location; the second Qwen check is a fallible semantic review,
not a formal entailment proof. A failure or invalid response is not reported as a
successful extraction of zero facts. Rejected proposals have safe reason codes.

BGE-M3 ranks possible pairs in RAM using cosine similarity, without a merge
threshold. `--top-k` defaults to 5 and accepts 1..20. Exact matches within the
local candidate pool bypass ranking; remaining pairs require contextual review.
`compared_pairs`, `available_pairs` and `exhaustive` describe candidate-pool coverage.
Main lookup is always non-exhaustive: unindexed/unretrieved Memories are not covered.
All providers are loopback-only, reused and closed on success, failure or cancellation.
No DB transaction remains open during Qwen or embedding generation.

The neutral `application.intelligence` package exposes `SourceDocument`,
`MemoryIntelligenceService.extract/preview`, `ExtractionResult`, `FactCandidate`,
`RelationSuggestion` and `PreviewResult`. It accepts provider ports, with no SQL,
filesystem, web-framework or persistence dependencies. A host runtime owns provider
lifecycle and optional canonical reads. This boundary can support future clients;
MCP, synchronization, recursive RAG and persistence are outside this preview.

The durable synthetic fixture contains 30 texts (15 PL/15 EN) and 24 labeled pairs.
Protocol/validation tests use controlled providers and do not measure Qwen accuracy.
Run the real model quality measurement separately (synthetic sources only):

```powershell
$env:KULAI_RUN_OLLAMA_INTEGRATION = "1"
$env:KULAI_RUN_LLM_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest -q -s backend\tests\integration\test_memory_intelligence_models.py
```

Missing models/service are explicitly NOT RUN; no downloads are attempted. Annotation
matches are limited type/technology checks, not full semantic accuracy. Inspect
reported misses and model outputs. An optional `KULAI_INTELLIGENCE_EVALUATION_OUTPUT`
path outside the repo saves synthetic evaluation details. Owned PostgreSQL tests
require `KULAI_RUN_POSTGRES_INTEGRATION=1`; all fixture writes/migrations are confined
to marker-owned databases, with unchanged full snapshots during preview.

Task reports overwrite `wynik.txt`. Run `scripts/export_repo.py` yourself when a
fresh `kod_repo_do_analizy.txt` is needed; preview never runs the exporter.

## Persistent facts in owned test storage (TASK 5A.2)

`application.fact_memory` separates staging, human review and approved retrieval
from the existing read-only intelligence preview. There are no new public routes,
NOTE hooks or startup migrations. The normal application does not import the new
PostgreSQL adapter. Main fact persistence is deliberately unavailable.

The third Alembic graph is `backend/facts_alembic.ini`, with current head
`kulai_facts_0002` (v1 was `kulai_facts_0001`) and version table `kulai_facts_version`. Canonical remains
`kulai_memory_0004`; security remains `kulai_security_0003`. Fact migrations require
an explicitly supplied connection and matching `OwnedTemporaryDatabase` capability.
The migration environment verifies the actual database name and marker before DDL.
Plain Alembic invocation, offline SQL and main installation are rejected. The
explicit `upgrade_owned_facts(owned, config=...)` never runs during startup.

Seven v1 fact tables store logical sources, source/analysis versions, independent
fact identities, candidate versions, immutable approval versions, relation
suggestions and append-only decisions. Source metadata includes revision/hash,
observed row token and a static pipeline/prompt fingerprint. Memory source rows
store no extra full-text snapshot; evidence and claims remain private derived data.
Local text sources require explicit registration with a UUID in the test adapter.

Each source version accepts one complete analysis. Reprocessing unchanged input
returns its original candidates, including their current decisions; changed Qwen
wording/order cannot reset or overwrite an approved/rejected/revoked candidate.
New relations only attach to matching original claims and subject scopes. Recovery
of missed facts through forced reanalysis is future work, not an implicit override.
After a source change, a new analysis creates new PENDING versions. An explicit
`RevisionBinding` can attach a new candidate to a prior fact identity from that
same source. No identity or content merge is inferred by Qwen/BGE.

The neutral `FactMemoryService` provides `list_pending`, `get_candidate`, `approve`,
`reject`, `revoke_approval`, `list_approved`, relation review and bounded decision
history. Decisions require an opaque principal minted by the trusted local adapter,
a UUID request key and an expected lock version. A JSON `reviewer=admin`, a string
or a principal from another scope is rejected. The local reviewer identity derives
from the trusted process login, not from a request. This is a test trust boundary;
it is not Android authentication or a production authorization system.

Transitions are PENDING -> APPROVED/REJECTED and APPROVED -> REVOKED. There is no
automatic reopening or AI approval. SQL constraints/triggers protect immutable
candidate/source/approval content and decision history. Human approval rechecks
evidence and semantic support through the existing intelligence verifier, without
holding a database checkout. The committing transaction locks the current source,
rechecks revision/hash/evidence/source token and stored candidate, then atomically
writes status, approval and audit. Decision request keys serialize retries;
optimistic versions and row locks protect competing decisions. Failed COMMIT
invalidates the connection, and retry/readback uses the same durable request key.
A replayed receipt is historical operation evidence, never restoration of a fact.

`list_approved` only returns approved versions with currently valid sources.
EDIT or ARCHIVE hides old versions immediately. The owned prototype also binds
Memory versions to a durable generation since TASK 5A.4: ARCHIVE followed by
RESTORE cannot silently restore a previous approval even if revision/content
match. PostgreSQL row triggers atomically invalidate versions, revoke approved
facts and approved touching relations, and append technical audit during the
canonical mutation. `reconcile_memory` remains an idempotent compatibility helper;
correctness no longer depends on calling it. The former `xmin` implementation
is retained only in the v1-to-v2 migration's one-time legacy validation.

Sources reference Memory with ON DELETE CASCADE. Existing canonical DELETE removes
all fact source versions, candidates, approvals and related suggestions touching
that source, including rationales that could quote its content. Triggers on the
new fact tables retain only opaque target/request IDs, action, reviewer identity,
time and counters. They retain no quote, source text, source hash or free-text
decision note. Local sources have an explicit `forget_local` operation; a technical
retirement prevents the same UUID from silently recreating deleted data. This is
logical database erasure; WAL, backups and physical storage retention still need
a separately reviewed privacy policy before production. No fact vector index is
created here. No canonical/security columns or migration heads are changed.

Approving a fact never approves a relation. Relation approval requires both endpoint
versions to be approved and valid, and still does not revoke/delete an older fact.
Revoked/invalid/deleted endpoints are excluded from approved relation retrieval.

Use `open_owned_fact_memory(owned, config=..., intelligence=...)` only inside a
trusted local test process. It yields `.service`, an opaque `.principal` and
owned-only `.storage` helpers. `.analyze_and_stage(runtime, memory_ids)` borrows the
existing `MemoryIntelligenceRuntime`; it releases source reads before extraction
and only stages after successful preview. Failed/cancelled inference stages nothing.
The caller owns the borrowed intelligence provider lifecycle. While a runtime is
open, `runtime.intelligence` exposes its existing service for revalidation without
creating extra providers. These helpers are not intended to accept arbitrary
untrusted client calls or database URLs.

After explicit owned schema installation and creating synthetic source Memories,
the trusted local host can stage and separately execute a user's decision:

```python
async with MemoryIntelligenceRuntime(settings=settings) as runtime:
    async with open_owned_fact_memory(
        owned, config=config, intelligence=runtime.intelligence,
    ) as facts:
        preview, staged = await facts.analyze_and_stage(runtime, (memory_id,))
        # Present candidates and evidence; obtain the user's choice separately.
        # chosen_id and displayed_lock_version come from that review interaction.
        receipt = await facts.service.approve(
            chosen_id, principal=facts.principal, request_id=request_uuid,
            expected_version=displayed_lock_version,
        )
```

Real PostgreSQL tests (synthetic Memories and marker-owned DBs only):

```powershell
$env:KULAI_RUN_POSTGRES_INTEGRATION = "1"
backend\.venv\Scripts\python.exe -m pytest -q backend\tests\integration\test_fact_memory.py
```

The tests use controlled Qwen/BGE providers for persistence paths; no expensive real
AI or WSS benchmark is needed. They cover lifecycle, idempotency, concurrent review,
source edits/deletion, privacy cascades, transaction failures, cancellation, stale
source checks, separate relation decisions, independent heads and owned DB cleanup.
Main activation requires a separate migration/review and complete lifecycle,
authorization, retention, backup/fingerprint and retrieval integration.

## Isolated Desktop Fact Review demo (TASK 5A.3)

Run in a separate PowerShell process from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\fact_review_demo.py
```

The window is labelled **TEST DATA**. It creates a new randomly named,
marker-owned loopback PostgreSQL database, installs the existing canonical and
fact graphs there, then stages four synthetic sources: Flask, the October switch
to FastAPI, a future MCP plan, and a Python preference. Existing database
configuration supplies connection credentials; main is never the target.
No ports, Ollama calls, downloads or normal Desktop hooks are started.
The demo uses explicitly controlled corpus providers through the existing
intelligence service. Its buttons call the real `FactMemoryService` and commit
real decisions to PostgreSQL; the corpus provider cannot authorize a decision.

Choose a status/type and select a row. Read the exact highlighted source, then
use APPROVE, REJECT or REVOKE. Plans remain labelled plans after approval.
The relations view shows both sources; approve both facts first, then make a
separate relation decision. Supersession does not delete/archive either Memory.
Pending counts, keyset pagination and REFRESH use bounded owned-only reads.
Unknown commit outcomes expose receipt lookup and explicit retry with the same
request ID. No new approval UUID is generated automatically.

The open panel polls each second. Since TASK 5A.4, background checks retain the
leased display: identical snapshots do not rebuild the table/source, and source
scroll/cursor/selection survive unrelated status/history changes. Manual refresh,
actions, read failure or invalidation clear private data. The two-second lease
still expires the display; late pre-expiry snapshots cannot repopulate it.
Invalidated/deleted source content is not restored by filters or request retry.
There can be a polling interval between an external mutation and its observation;
this demo does not provide instantaneous remote invalidation or secure RAM erasure.

Close the window to cancel work, close pools/providers and drop exactly the
generated database after ownership verification. Closing during provisioning
waits for provisioning to finish so its capability can be cleaned up. The Qt
window stays in its closing state until cleanup finishes. Never kill the process
as a normal exit. A forced process kill may leave an owned test DB requiring
separately verified cleanup. An ownership/cleanup failure stops safely and is
shown without private error text; no search-and-delete by prefix occurs.
Each demo launch starts fresh. Automated tests reopen windows against the same
retained owned DB to verify that decisions themselves are durable.

The normal Desktop and preview CLI remain unchanged. The future Android scope,
device binding and reviewer contract is documented in
[`docs/fact_review_android_contract.md`](docs/fact_review_android_contract.md).

## Durable source lifecycle in owned storage (TASK 5A.4)

Facts v2 adds `kulai_fact_memory_epochs`: immutable Memory UUID/incarnation,
monotonically increasing bigint generation, and optional deletion time. It stores
no Memory text, evidence or hashes. A generation row survives DELETE as a minimal
retirement; reinserting the same Memory UUID is rejected. Runtime validity uses
`epoch:<incarnation>:<generation>`, canonical revision/hash, active-source state
and evidence. It never reads transaction IDs.
Owned Memory document labels also include the captured generation. Stage rejects
an extraction made before an intervening ARCHIVE/RESTORE or content ABA even when
the current revision/hash are unchanged. This adds no field to the public API.

The explicit owned-only migration backfills existing source generations and
installs INSERT/UPDATE/DELETE/TRUNCATE guards on `memories`. Any actual row change
increments the generation; a no-op rewrite leaves it unchanged. Canonical
`revision` remains content versioning under its existing application semantics.
Triggers execute inside the caller's transaction, including direct SQL: rollback
undoes the canonical change, generation increment, revocation and audit together.
TRUNCATE is rejected; DELETE uses the existing privacy CASCADE. Normal Desktop
and API persistence functions do not need Python hooks.

Migration v1 -> v2 locks the owned canonical table for exclusive maintenance. It
converts still-verifiable legacy tokens once, preserving candidate/decision IDs
and valid approvals. Unverifiable legacy versions are invalidated with technical
audit, without dropping prior decisions. Runtime requires exact fact head v2;
opening a v1 adapter does not automatically migrate it. Canonical/security heads
remain 0004/0003 and main is never a migration target here.

Locks follow canonical Memory -> source/version/candidate -> approval/relation.
The adapter re-reads the epoch after acquiring the canonical row lock to avoid a
pre-wait joined snapshot. Multi-Memory commands lock canonical IDs in sorted order.
Arbitrary opposing SQL batches can still deadlock: PostgreSQL aborts a whole
victim transaction (including its fact effects). There is no automatic retry
with a fresh approval UUID. Bulk writers must use a consistent row-lock order
and explicitly handle transaction aborts.

Tests cover both approve/mutation lock orders, rollback, cancellation after
writes, actual deadlock rollback, unchanged-content revision ABA, adapter restart,
v1 migration, physical row rewrite and owned `VACUUM (FREEZE, ANALYZE)`.
Epochs solve normal lifecycle validity, not restoration of an older whole-DB
snapshot. The separate quarantine/retirement-ledger/reapproval plan is
[`docs/fact_restore_privacy_plan.md`](docs/fact_restore_privacy_plan.md).
No production recovery is enabled.

## Approved Fact-RAG in owned storage (TASK 5A.5)

`application.approved_facts.ApprovedFactsService` borrows the neutral
`ApprovedFactsReader` read/revalidate port. `OwnedApprovedFactsReader` only wraps
the existing marker-guarded storage with exact facts head `kulai_facts_0002`.
It joins active approvals, canonical Memory and durable epochs, and validates
revision, hash, source metadata, information type, subject scope and exact Unicode
evidence. PENDING/REJECTED/REVOKED, archived/deleted or invalidated versions cannot
enter retrieval. Local file-only sources without a canonical Memory are excluded.
ASK executes read-only, repeatable-read transactions followed by rollback.

The bounded collection defaults to 50 candidates (maximum 100), ordered by
candidate UUID; optional exact project/subject-scope filters apply before the
limit. Existing BGE-M3 ranks candidates in RAM using cosine similarity, in batches
of eight. There is no persistent fact vector index, cache, migration or score
threshold. Similarity only ranks relevance. `top_k` is 1–20; query length is at
most 4,000 characters. Candidate embedding JSON is capped at 120,000 characters
(configurable 2,000–200,000), context JSON at 16,000 (512–48,000). Only complete
fact/evidence blocks are included. Limits are characters, not token counts.
`partial_coverage`/`partial_reasons` report candidate, ranking, relation and context
limits; a small collection is not a complete knowledge base.

The pipeline embeds the question, reads one consistent collection, embeds/ranks
outside any transaction, then revalidates selected facts and relation snapshots.
It constructs the prompt synchronously after that read returns. Every context
block contains the full fact identity/version, candidate and Memory IDs,
revision, source SHA-256, incarnation and generation, plus a local `F1`–`F20`
citation token. Qwen selects tokens in strict JSON; the application resolves the
full immutable references from the supplied context, never from model-supplied
identity metadata. Unknown tokens, forged reference objects, duplicate/missing
citations and incomplete conflict/supersession
endpoint citations fail closed. A second structured Qwen call checks semantic
grounding, types, scopes, negation and time. Plans never establish completion;
October without a year stays October without a year. Suggested relations retain
their separate review status and never delete canonical Memory.

After generation/verification, a fresh bounded read revalidates **all** context
facts and supplied relations. Any invalidation yields a safe error with no draft
answer. This final consistent snapshot is the validity boundary: a mutation
committed after its snapshot was established can occur before transport or after
delivery. Returned objects are detached; answers already delivered cannot be
retracted. No PostgreSQL lock or transaction spans BGE/Qwen. No automatic provider
or decision retries occur. Structured validation proves identity/provenance, not
semantic truth; the verifier remains probabilistic, especially for correlated
errors and adversarial evidence. Errors and repr omit private text.

Run from the repository in PowerShell:

```powershell
.\.venv\Scripts\python.exe .\scripts\ask_approved_facts_demo.py
# Existing installed local models, no downloads:
.\.venv\Scripts\python.exe .\scripts\ask_approved_facts_demo.py --real-models
```

Each launch provisions a fresh owned synthetic DB, installs the existing graphs
there, stages four known synthetic annotations, and starts with **zero approvals**.
The default providers are explicitly CONTROLLED fixtures, including a fixed
grounding oracle; they measure contracts, not model quality. `--real-models` uses
real BGE/Qwen for ranking, answering and grounding. Extraction and approval
verification still use the labelled synthetic annotation provider.

Use `list`, copy a candidate UUID, then `approve UUID`, `reject UUID` or
`revoke UUID`. These invoke real `FactMemoryService` methods with the local opaque
principal, displayed lock version and fresh request UUID. Reviewer identity
cannot be entered as text. Use `relations` and `approve-relation UUID` for separate
relation review; both endpoints must already be APPROVED. `ask QUESTION` displays
the answer, coverage and full source/type/evidence citations. ASK never stages or
approves. `quit`/EOF closes providers/connections and drops only this session's
verified DB. An uncertain decision stops the demo without automatically retrying
with a new request ID. There is no resume mode for somebody else's database.

This mode is opt-in and disconnected from normal ASK, NOTE, Desktop and HTTP.
Tests: `backend/tests/test_approved_facts.py` and
`backend/tests/integration/test_approved_facts_postgres.py`; enable
`KULAI_RUN_POSTGRES_INTEGRATION=1` for the latter. The short real-model test in
`test_approved_facts_models.py` additionally requires
`KULAI_RUN_OLLAMA_INTEGRATION=1` and `KULAI_RUN_LLM_INTEGRATION=1`; missing existing
models are NOT RUN, never downloaded. Main remains read-only without facts.

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
Memory UUID, a session factory and optional `expected_revision`. When supplied,
the revision must match the canonical row under its lock before any mutation;
a missing/stale row fails with `memory.revision_conflict`. It retires ingestion
identity and deletes the canonical Memory, then
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

Deletion has Desktop Memory Library UI with mandatory verified backup; there is
no deletion CLI or public HTTP/WebSocket endpoint. Real deletion and
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
provides EDIT, ARCHIVE, and RESTORE controls, plus verified-backup hard DELETE.
There is no public HTTP/WebSocket lifecycle endpoint yet.

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
