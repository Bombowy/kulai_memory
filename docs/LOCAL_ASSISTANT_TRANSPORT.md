# Local Memory history and assistant transport (6C.1)

Start with `.venv\Scripts\python.exe scripts\dev.py`. It binds exclusively to
`127.0.0.1:8000`; this development transport has no authentication. History/detail
intentionally return canonical user data to the local client. Do not expose the
port to a network. LAN/WSS/auth/pairing remain TASK 4D. No CORS wildcard is added.

Desktop stays in process. The server performs no TTS and stores neither questions
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
| 400 | Invalid assistant request. |
| 422 | Invalid assistant request. |
| 404 (detail) | Memory not found. |
| 503 | Memory assistant is unavailable. |
| 500 | Memory assistant could not complete the operation. |

Validation never echoes query/input and errors contain no content/credentials.
Normal server logging records request structure, not bodies or Memory content.

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

## Future Android 6C.2

- HTTP_BASE_URL (debug): `http://127.0.0.1:8000`
- VOICE_NOTE_WS: `ws://127.0.0.1:8000/ws/memory`
- VOICE_ASSISTANT_WS: `ws://127.0.0.1:8000/ws/assistant`

Continue `adb reverse tcp:8000 tcp:8000` for a USB-connected debug device.
6C.2 can add history/detail and text/voice assistant screens and BuildConfig URLs.
This task changes no Android sources, release networking or security settings.
It adds no lifecycle endpoints, conversation history or TTS transport.
