# KulAI Memory backend

The host application exposes a transport-neutral application core in
`kulai_memory.application`. A desktop adapter can call `VoiceSession` directly
in process, while the server adapter forwards the same typed events as
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

The desktop adapter in `kulai_memory.desktop` now orchestrates this path in
process. Its PySide6 UI never calls localhost HTTP. A dedicated worker thread
owns one asyncio event loop, one reusable Whisper provider, and the database
pool, so model load, inference, and PostgreSQL work do not block the Qt UI
thread.

The `Memory` domain and `MemoryService` also live in `kulai_memory.application`.
They depend only on the `MemoryRepository` port. The PostgreSQL implementation,
`PostgresMemoryRepository`, lives in `kulai_memory.persistence` and receives a
caller-owned SQLAlchemy `AsyncSession`. It flushes changes but never commits or
rolls back; the desktop or server use case owns that transaction boundary.

The host `memories` table stores the durable text and JSON metadata without an
embedding. Embeddings are stored through the reusable vector store, using
`str(Memory.id)` as `record_id` in `kulai_memory.memories.v1`. Voice runtimes
commit canonical Memory first, close that session, then ensure its compatible
BGE-M3 vector in a separate short transaction. Duplicate retries and bounded
startup reconciliation repair missing vectors; indexing failure leaves Memory
durable and exposes a distinct retry status. Incompatible vectors require an
explicit manual reindex and are never automatically overwritten.

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

The core FastAPI runtime provides `/health` plus the local-only
`/ws/memory` voice-memory adapter. KulAI modules remain pinned by
`../kulai.project.json` and the `../vendor/kulai_modules` submodule.

ASGI entry point: `kulai_memory.main:app`. Copy `backend/.env.example` to `backend/.env` before local runtime. After Studio bootstrap, run the root `scripts/dev.py` with the project `.venv` Python.

Local text RAG is available through the Desktop **Ask Memory** panel and the
read-only `scripts/ask_memory.py` CLI, documented in the root README.
`application.rag` uses only neutral retrieval and LLM contracts. Desktop reuses
the canonical `rag_runtime.MemoryRagRuntime`. In Desktop, this runtime borrows
one shared BGE provider also used by automatic indexing and owns one reusable
local Qwen provider. In the CLI, the runtime owns both providers.
`KULAI_LLM_MODEL=qwen3.5:9b` shares the loopback `KULAI_OLLAMA_BASE_URL` setting.
Canonical retrieval closes its short read-only snapshot before structured Qwen
generation. Bounded untrusted JSON evidence contains only IDs, ranks and content;
answers require validated citations or return canonical insufficient context.
Zero evidence skips the LLM. There is no reviewed score cutoff/reranker,
voice-question RAG, TTS, Android RAG UI, WebSocket RAG, or multi-turn conversation
yet. Tests use only owned DBs with synthetic evidence.

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

Install and launch the desktop alpha from the repository root:

```text
.venv\Scripts\python.exe -m pip install -e ".\backend[desktop]"
.venv\Scripts\python.exe scripts\db_doctor.py
.venv\Scripts\python.exe scripts\desktop.py
```

The desktop and WebSocket server require the canonical `large-v3`, `cuda`,
`int8_float16`, and VAD-enabled Whisper settings. `KULAI_CUDA_DLL_DIR` can
identify a local CUDA 12 DLL directory. It is validated and added only to the
current process PATH; there is no CPU fallback or system PATH change. The microphone adapter uses
`sounddevice.RawInputStream` to write a private temporary 16 kHz, mono, PCM16
WAV and removes only files that it created.

One logical recording receives one UUID before capture starts. If PostgreSQL
fails after successful STT, the process retains the `TranscriptionResult`,
ingestion UUID, and voice-session UUID. `RETRY SAVE` uses those same values and
does not run STT again. `CREATED` and `DUPLICATE` clear this pending state;
empty VAD output reports `SKIPPED_EMPTY` and creates no Memory.

The WebSocket v1 adapter accepts one note per connection. A client sends a
typed `recording.start` command containing a stable ingestion UUID, binary
`pcm_s16le` chunks (16 kHz, mono), and `recording.stop`. Frames are limited to
256 KiB and total audio to 19,200,000 bytes. The server streams the PCM into an
owned temporary WAV, serializes inference through one process-wide large-v3
provider, and removes the WAV after STT or disconnect.

Canonical events are emitted in operation order. An empty final transcript
closes the connection without Memory events. A recoverable save failure leaves
the socket open for `memory.retry`, which reuses the same transcript and IDs.
The server closes cleanly after `memory.saved`. The adapter is bound only by the
existing localhost development launcher; LAN exposure, authentication, and the
Android client belong to TASK 4C.
