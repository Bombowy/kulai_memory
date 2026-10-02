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

`VoiceSession.start()` emits `session.ready`. Future transcription, memory, RAG,
and assistant operations will use the event catalog already defined by the
core. No HTTP server is required for in-process use.

The `Memory` domain and `MemoryService` also live in `kulai_memory.application`.
They depend only on the `MemoryRepository` port. The PostgreSQL implementation,
`PostgresMemoryRepository`, lives in `kulai_memory.persistence` and receives a
caller-owned SQLAlchemy `AsyncSession`. It flushes changes but never commits or
rolls back; the desktop or server use case owns that transaction boundary.

The host `memories` table stores the durable text and JSON metadata without an
embedding. TASK 5 will write embeddings through the reusable vector store, using
`str(Memory.id)` as its `record_id` under a stable memories namespace.

Host package: `kulai_memory`.

The core FastAPI runtime and `/health` endpoint are generated. KulAI modules remain pinned by `../kulai.project.json` and the `../vendor/kulai_modules` submodule. Auth and business wiring are not generated yet.

ASGI entry point: `kulai_memory.main:app`. Copy `backend/.env.example` to `backend/.env` before local runtime. After Studio bootstrap, run the root `scripts/dev.py` with the project `.venv` Python.
