# V2.4 conversation API

This is the transport for the existing V2.3 `ConversationService`. It requires
canonical schema `kulai_memory_0005` and existing SECURE pairing/session
credentials (security `kulai_security_0003`). No migration runs at startup.
The running V1 and its Nginx configuration have not been updated by this task.

All requests use the existing `Authorization: Bearer ...` and optional
`X-Kulai-Device-ID` headers. Credentials never belong in URLs or WebSocket
subprotocols. The current shared personal database uses the same authorization
scope as canonical Memories; conversation UUIDs do not introduce user accounts.

| Method | Backend path | Existing permission |
| --- | --- | --- |
| POST | `/api/conversations` | `assistant.ask` |
| GET | `/api/conversations` | `memory.read` |
| GET | `/api/conversations/{conversation_id}/messages` | `memory.read` |
| POST | `/api/conversations/{conversation_id}/ask` | `assistant.ask` |
| GET Upgrade | `/ws/assistant` | `assistant.ask` |

## HTTP

Create with `{}` or `{"title":"Synthetic project"}` (the body may be omitted).
The 201 response contains `conversation_id`, optional `title`, `created_at` and
`last_activity_at`. The server assigns the conversation UUID. Titles are nonblank
when supplied, contain no NUL, and have at most 200 characters.

List with `limit=20` (1..100), optionally both `before_activity_at` (timezone-aware
ISO 8601) and `before_id` (UUID). The response is
`{"conversations":[...],"next_cursor":null}` or a `next_cursor` object containing
those two query parameters. Order is last activity descending, UUID descending;
each page is a separate read-only snapshot, not a frozen multi-request listing.

Read messages with `limit=50` (1..100), optionally `before_sequence` (positive
64-bit integer). The response is `{"messages":[...],"next_before_sequence":null}`
or an exclusive cursor for older messages. The selected bounded window is always
chronological. Messages expose their UUID, conversation/request UUIDs, role,
content, timestamp, sequence, user `input_kind` and assistant `memory_ids`.

Send a question to an existing conversation:

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "question": "How can we promote it?",
  "input_kind": "text"
}
```

`request_id` is required; `input_kind` is `text` (default) or `voice`. Questions
must be nonblank, contain no NUL, and have at most 10,000 characters. The 200
response is `{"turn":{...},"created":true}`. The turn contains conversation and
request UUIDs, `turn_number`, and complete `user` / `assistant` messages. Repeating
the exact request returns the original turn with `created:false`, without Qwen.
Reusing its UUID with a different conversation, question or input kind is 409.
Keep the UUID when retrying after an uncertain network outcome; use a new UUID
for a new question.

Authentication failures are 401; missing permission is 403; missing conversation
is 404; invalid input is 422; generation failure is 502; unavailable runtime or
persistence is 503. Error responses do not echo input or provider diagnostics.
Responses use the existing no-store policy. Generation errors, cancellation and
disconnect before generation completes do not append a partial turn. A disconnect
after a committed complete turn may leave that turn stored; retry its UUID.

## Optional conversation voice mode

Keep the V1 schema/version, PCM contract and `question.stop` unchanged. Add both
UUIDs to `question.start` to opt into V2:

```json
{
  "schema_version": 1,
  "type": "question.start",
  "conversation_id": "00000000-0000-4000-8000-000000000002",
  "request_id": "00000000-0000-4000-8000-000000000003",
  "audio": {"encoding":"pcm_s16le","sample_rate":16000,"channels":1}
}
```

Send the existing binary PCM frames, then the existing schema-1
`question.stop`. Events are `session.ready`, `transcript.final`, `rag.started`,
`conversation.completed` with increasing sequence numbers, followed by close
1000. The last event's `payload` is the same `TurnWrite` object as HTTP, including
the durable conversation/request identities. Its envelope `session_id` is only
the ephemeral socket session. Each socket carries one question; subsequent
sockets and text requests reuse the durable conversation UUID.

Omitting both IDs preserves V1 exactly, including `assistant.completed` and its
original payload. Supplying only one or an invalid UUID is a protocol error
(1002). V1 `top_k` still applies to classic ASK; V2 uses the existing V2.3 service
settings (retrieval top-k 5), without changing Qwen/RAG logic.

The voice request UUID identifies the original recording. A retry of an already
committed voice turn returns its stored transcript and pair with `created:false`,
without Whisper or Qwen; start/stop without PCM is enough for this replay.
New recordings must receive new UUIDs. Empty unsaved input/transcription closes
normally without saving a turn. A UUID belonging to another conversation or a
text turn fails with `conversation.request_conflict` (1008). Missing conversation
is `conversation.not_found` (1008), generation failure is
`conversation.generation_failed` (1011), persistence failure is
`conversation.unavailable` (1013). Existing authentication is checked before
acceptance and again when receiving frames, including after credential revocation.
Temporary WAVs are cleaned up; audio is never stored in conversation tables.

## Future Nginx allowlist change — not deployed

Only the following new public rules will be required, after separate review and
approval of the actual Nginx diff:

| Method | Public path | Upstream path |
| --- | --- | --- |
| GET, POST | `/memory/api/conversations` | `/api/conversations` |
| GET | `/memory/api/conversations/{canonical-UUID}/messages` | `/api/conversations/{canonical-UUID}/messages` |
| POST | `/memory/api/conversations/{canonical-UUID}/ask` | `/api/conversations/{canonical-UUID}/ask` |

Canonical UUID matching is `[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}`.
`GET Upgrade /memory/ws/assistant` already exists; V2 changes only its optional
payload. The backend mTLS ingress permits precisely the new HTTP method/path
pairs, rejecting other methods, trailing slashes, unknown subpaths and encoded
path aliases. Public prefix removal, mTLS verification, header filtering,
authentication and disabled automatic proxy retries must retain the V1 behavior.
No existing Nginx fragment, production process, Android client or database was
changed for this transport task. NOTE, MEMORY, classic ASK and TTS retain their
existing endpoints and behavior. Conversation text never becomes canonical
Memory automatically.
