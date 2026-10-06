# KulAI Memory

KulAI Studio project `kulai_memory`.

## Reproducibility

- Source template: `blank`
- Current pinned KulAI commit: `925b08a13aab9a62e080103185446543a7795794`
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
