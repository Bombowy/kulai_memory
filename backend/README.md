# KulAI Memory backend

The host application exposes a transport-neutral application core in
`kulai_memory.application`. A desktop adapter can call `VoiceSession` directly
in process, while a future server adapter can forward the same typed events as
JSON over WebSocket. Transport adapters are responsible only for translating
their input and output; session behavior stays in the application core.

```python
from kulai_memory.application import EventSink, VoiceSession


class DesktopEventSink:
    async def emit(self, event):
        # Forward the Python event object to the desktop UI boundary.
        ...


async def run_session(event_sink: EventSink) -> None:
    session = VoiceSession(event_sink=event_sink)
    await session.start()
    await session.close()
```

`VoiceSession.start()` emits `session.ready`. An active session can run one-shot
speech-to-text through the provider-neutral `TranscriptionService` in
`kulai_memory.application.transcription`. It returns the complete
`TranscriptionResult` and emits one `transcript.final`; an empty final text is a
valid result for silence. Real `transcript.partial` events are not implemented
because the current provider has no streaming partial contract.

The concrete `WhisperTranscriptionProvider` is created outside the application
core by `kulai_memory.whisper_provider`. Its host configuration is read from:

- `KULAI_WHISPER_MODEL` (default `large-v3`)
- `KULAI_WHISPER_DEVICE` (default `cuda`)
- `KULAI_WHISPER_COMPUTE_TYPE` (default `int8_float16`)
- `KULAI_WHISPER_VAD_FILTER` (default `true`)

The host enables the provider's speech detector by default so controlled
non-speech input can produce an empty transcript instead of hallucinated text.
An empty `transcript.final` remains a valid result and does not imply an error.

For `cuda`, the CUDA 12 runtime libraries required by CTranslate2 must be
discoverable through the process `PATH`. The host does not fall back to CPU.

Run the real local path with `python scripts/stt_smoke.py`, optionally adding
`--audio <path> --language pl`. The no-argument form creates and removes a short
temporary PCM WAV and checks model load, decode, inference, result mapping, and
event delivery. Run the opt-in pytest integration with
`KULAI_RUN_WHISPER_INTEGRATION=1 python -m pytest backend/tests/integration/test_whisper.py`.
`KULAI_WHISPER_AUDIO` can point that test at a local speech sample. The real
integration also checks temporary silence, click, low-level noise, and tone
fixtures without writing audio files into the repository.

TASK 3A does not persist audio or transcripts and does not call `MemoryService`.
A later mobile/WebSocket adapter will translate incoming audio bytes into the
same application request used by the in-process desktop path. No HTTP server is
required for in-process use.

The `Memory` domain and `MemoryService` also live in `kulai_memory.application`.
They depend only on the `MemoryRepository` port. The PostgreSQL implementation,
`PostgresMemoryRepository`, lives in `kulai_memory.persistence` and receives a
caller-owned SQLAlchemy `AsyncSession`. It flushes changes but never commits or
rolls back; the desktop or server use case owns that transaction boundary.

The host `memories` table stores the durable text and JSON metadata without an
embedding. TASK 5 will write embeddings through the reusable vector store, using
`str(Memory.id)` as its `record_id` under a stable memories namespace.

The provider-neutral `TranscriptMemoryIngestionService` is the persistence stage
after STT/VAD. It accepts a `TranscriptionResult`, a voice `session_id`, and a
client-generated `UUID ingestion_id`. Empty or whitespace-only transcripts return
`skipped_empty` and never call the repository. A non-empty transcript is stored
once: an identical retry returns the original Memory as `duplicate`, while reuse
of the UUID with different content or a different session raises a controlled
idempotency conflict. Repeating the same spoken text under a new ingestion UUID
creates a distinct Memory.

PostgreSQL enforces `memories.ingestion_id` as `NOT NULL UNIQUE`. The adapter uses
an atomic insert-on-conflict operation but keeps the existing caller-owned
transaction boundary. Ingestion stores provider-neutral STT diagnostics without
raw audio or segment text. Language metadata is diagnostic and is never used as
a speech-presence or acceptance rule. Persistence events remain the responsibility
of a future session orchestrator so `VoiceSession` stays independent of Memory.

Host package: `kulai_memory`.

The core FastAPI runtime and `/health` endpoint are generated. KulAI modules remain pinned by `../kulai.project.json` and the `../vendor/kulai_modules` submodule. Auth and business wiring are not generated yet.

ASGI entry point: `kulai_memory.main:app`. Copy `backend/.env.example` to `backend/.env` before local runtime. After Studio bootstrap, run the root `scripts/dev.py` with the project `.venv` Python.

Local PostgreSQL is started only through `scripts/postgres.py`, which passes
`backend/.env` explicitly to Compose. `scripts/db_doctor.py` performs read-only
schema and revision checks. `scripts/db_backup.py` creates a full PostgreSQL
custom-format dump, and `scripts/db_restore_smoke.py` restores it only into a
randomly named, process-owned temporary database. The canonical development
server is PostgreSQL 18. `scripts/db_backup_restore_drill.py` proves a non-empty
round trip using owned source and restore databases without writing fixtures to
the configured main database. See `../docs/DATA_SAFETY.md` for the invariants
that persistence, embeddings, and future RAG work must preserve.
`DATABASE_URL` must remain unset for the canonical local Compose workflow so
the backend and container use the same `DB_*` values from `backend/.env`.
