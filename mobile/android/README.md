# KulAI Memory Android Alpha — NOTE / ASK / MEMORY / CHAT

This native Kotlin/Compose client uses the configured KulAI Memory server and
existing SECURE pairing, including an HTTPS path prefix such as
`https://demo.kulai.eu/memory/`. V2.5 adds CHAT on the V2 feature branch; it is not
installed on the phone or deployed to the running V1 by this task.

The four tabs retain their in-memory state while switching and across Activity
recreation. There is no Room/SQLite/DataStore history or conversation database.
Process death clears UI state; opening CHAT again fetches the conversation list
and selected history from PostgreSQL through the API. The existing Note retry
cache does not provide restart recovery.

- **NOTE** keeps the existing `/ws/memory` v1 Voice Note flow. Bounded PCM16 audio
  is privately cached only while retry is possible. **RETRY NOTE** replays PCM
  with the same ingestion UUID; **RETRY SAVE** retries saving/indexing without
  another capture or transcription. Saved/empty/terminal notes remove their cache.
- **ASK** sends text to `POST /api/assistant/ask` with `top_k=5`, nonblank queries
  up to 10,000 characters and explicit `application/json; charset=utf-8` JSON.
  **ASK BY VOICE** opens `/ws/assistant`, waits for `session.ready`, streams the
  same 16 kHz mono PCM16 contract, then **STOP QUESTION** sends `question.stop`.
  Transcript → Searching → Answered shows the grounded answer and validated
  citations (rank, Memory UUID, score). Insufficient context shows the canonical
  answer with no sources. Silence shows **No speech detected**, without RAG.
  Questions have no ingestion UUID, retry messages, disk cache or Memory writes.
  Connection loss requires asking again. **CANCEL QUESTION** cancels a pending
  request/capture; a new text ASK cancels the previous text request.
  **SPEAK** and **STOP AUDIO** provide local Polish / English speech with
  **Speak answers automatically** enabled by default for both text and voice
  answers, including insufficient context. A common post-render trigger consumes
  each answer once; returning to ASK does not replay it automatically.
- **MEMORY** shows **ACTIVE** or **ARCHIVED** history, 20 items per page, with
  **REFRESH**, **LOAD MORE**, full detail and **BACK**. Pagination uses exactly
  `before_created_at` + `before_id`, ordered by descending timestamp then UUID;
  it does not use OFFSET. Previews are derived by the backend. After a successful
  Note save, history is marked stale and refreshed on its next visit or REFRESH.
  There are no Android lifecycle mutation controls.
- **CHAT** uses only the [V2.4 conversation API](../../docs/CONVERSATION_API.md).
  **NOWA ROZMOWA**, a paginated conversation list and selection open a scrollable
  server history. **WYŚLIJ** and **MIKROFON / STOP** share the same conversation
  UUID; each new question gets a request UUID. **PONÓW** reuses it. If a voice
  transcript arrived, retry uses the same UUID/input-kind through HTTP; otherwise
  WS start/stop checks for the saved pair without recording again. No CHAT audio
  is cached. If no saved result is available, refresh or explicitly record a new
  question. Uncertain request identities are held in memory while the process
  lives; process restart fetches server history and never automatically resends.
  Changing conversations cancels HTTP/WS/capture and ignores stale callbacks.
  Switching tabs retains state and paired credentials. CHAT uses the existing
  OkHttp boundary, shared microphone gate and single PL/EN TTS engine. **SPEAK**
  reads answer prose only; the auto-read checkbox and **STOP AUDIO** are shared
  with ASK. Loading old history does not auto-read old answers.

CHAT requires schema `0005`, a V2 server and the separately approved future
Nginx allowlist change. This task does not migrate `kulai_memory_mvp`, change
Nginx or switch the live V1. The debug APK remains
`app/build/outputs/apk/debug/app-debug.apk`; it is built without installing it.
`testDebugUnitTest` covers controlled HTTP/WS contracts, retries, capture drain,
history recovery, TTS and V1/pairing regressions. The opt-in
`backend/tests/integration/test_android_chat_postgres.py` fixture runs the complete
Android JVM suite against actual SECURE FastAPI and a marker-owned PostgreSQL
0005 database (`KULAI_RUN_POSTGRES_INTEGRATION=1` and
`KULAI_RUN_ANDROID_INTEGRATION=1`). Only synthetic inference/data are used; the
fixture passes temporary test credentials in the child environment and checks
stored turns/model-call counts so skipped tests cannot look successful. The
phone microphone, layout and offline voices still require a later physical test.

HTTP and assistant WS parsers reject unknown/missing fields, wrong types, invalid
UUIDs/dates/citations and unexpected assistant events. The assistant sequence
starts at 1 and remains contiguous in one session. HTTP and WS share one answer
parser. Response errors are authored messages, never raw bodies or exceptions.
Cancellation cancels OkHttp calls; jobs plus generation ownership prevent late
HTTP/WS responses from overwriting current state. Network and JSON decoding run
outside the Main thread. HTTP responses are bounded to 2 MiB and closed even on
failure/cancellation; connect/read/whole-call timeouts are 10/210/240 seconds.

A shared capture gate reserves the microphone before connecting and releases
it after AudioRecord drains. Note and Voice ASK cannot capture simultaneously,
including rapid double taps and cancellation. Both voice starts require
microphone permission; text ASK, history and detail work without it. A button
in either voice area requests permission. No question audio is retained.

## Local Polish / English speech

One Android `TextToSpeech` instance belongs to the ViewModel and uses Application
Context. Startup audits installed voices without speaking or downloading data.
Selection requires exact offline `pl-PL` and `en-US`, rejects network-required
or not-installed voices, sorts by voice ID and picks the first eligible voice
for each locale. The selected offline PL/EN IDs appear under the speech controls.
If either locale is missing, **TTS unavailable** appears; NOTE/ASK/MEMORY still work.
Install/configure offline voice data manually in Android TTS settings and restart
the app to repeat the audit. The app starts no download or cloud TTS request.
Android 11+ TTS service visibility uses a manifest queries intent, as required by
the [TextToSpeech API](https://developer.android.com/reference/android/speech/tts/TextToSpeech).

The client sends only the final answer to `POST /api/assistant/speech-plan`.
The server reuses canonical `application.speech.plan_speech`, its long-lived Qwen
and the same AI lane; it opens no DB transaction and invokes no BGE/Whisper.
The canonical insufficient answer uses a direct PL plan with no Qwen call.
Both ends enforce 1..64 nonempty PL/EN segments, answer <=6,000 characters and
exact concatenation, preserving every character, space and punctuation. There
is no client language heuristic, translation, text repair or whole-answer PL
fallback. Android uses a conservative UTF-16 character bound.

Before every utterance, TTS sets the selected voice and verifies its active ID.
Segments play sequentially, awaiting UtteranceProgressListener completion. Text
longer than Android's native input bound is split into exact chunks in the same
language, preserving surrogate pairs. Unique generation/segment/chunk IDs reject
stale callbacks. Initial audit is bounded to 10 seconds, an utterance to 120
seconds, and the speech operation to 600 seconds; the existing HTTP timeout also
bounds planning. No app audio file/cache or synthesizeToFile is used.

**STOP AUDIO** cancels planning/utterances, flushes TTS and retains answer/sources.
**SPEAK** can repeat a valid answer with auto speech off or after completion.
New Text ASK, Voice ASK or Voice Note stops and drains previous speech before
starting. Playback continues across tabs; STOP AUDIO is available on returning
to ASK. Speech does not start into an active microphone capture or replacement
operation. TTS requires no microphone permission. A planning/TTS failure displays
**Could not speak answer.** without clearing the grounded answer or citations.
ViewModel shutdown cancels speech and shuts down the native TTS instance once.

Physical device review should verify selected PL/EN voice IDs and actual
pronunciation, including PL→EN→PL and EN→PL→EN. JVM tests use a fake platform
boundary to exercise production voice selection, verification, ordering and
callback ownership; they do not assess acoustic quality.

Debug BuildConfig URLs:

```text
MEMORY_HTTP_BASE_URL=http://127.0.0.1:8000
VOICE_WS_URL=ws://127.0.0.1:8000/ws/memory
ASSISTANT_WS_URL=ws://127.0.0.1:8000/ws/assistant
```

The default/release URLs are HTTPS/WSS placeholders. Cleartext is enabled only
in `src/debug/AndroidManifest.xml`; main/release does not enable it. These
placeholders do not implement LAN/WSS. Backend remains bound to loopback;
ADB reverse provides local access. Debug explicitly selects `LOCAL_DEV`; release
selects `SECURE` and refuses private requests without credentials. The shared
`CredentialProvider` supplies a nonblocking, thread-safe credential snapshot to
HTTP and both WS clients. Tokens are sent only in `Authorization: Bearer`;
`X-Kulai-Device-ID` is a public identity claim validated against the token owner.
Server Connection supports administrator-approved pairing and encrypted credential
storage. For the shared demo host, enter `https://demo.kulai.eu/memory/`, disable
LOCAL_DEV, and apply the server before pairing. Root URLs remain supported.

`ServerOrigin` accepts a root URL or the literal `/memory/` service prefix (also
accepted without its trailing slash). Other paths, encoded/ambiguous paths,
userinfo, queries and fragments are rejected. HTTP, NOTE/ASK WebSockets, pairing,
session refresh and TTS speech-plan all append to that same service base.
`RequestAuthentication` binds credentials to the configured scheme, host, port
and service prefix. It blocks requests outside that prefix, including other
paths on the same domain, and
rejects credentials in URL query parameters. HTTP/WS redirects are disabled.
Remote cleartext and remote `LOCAL_DEV` are rejected. Loopback HTTP/WS remain
available for ADB reverse. The `/memory/` Nginx candidate consists of
`ops/remote_access/nginx/memory-path.http.conf` (existing `http {}`) and
`memory-path.server.conf` (existing demo HTTPS `server {}`). Deployment still
requires separate approval; backend `public_origin` stays pathless.
Provider errors and 401/403 produce authored, secret-free errors. Credential
objects redact their string representation. No token appears in BuildConfig,
resources or logs; `SECURITY_MODE` contains only the public mode name.
See [the server contract](../../docs/LOCAL_ASSISTANT_TRANSPORT.md).

Build/install from `mobile/android`:

```text
gradlew.bat testDebugUnitTest
gradlew.bat assembleDebug
adb reverse tcp:8000 tcp:8000
adb reverse --list
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Start the backend separately from the repository root:

```text
.venv\Scripts\python.exe scripts\dev.py
```

Manual smoke after review:

1. NOTE: record, STOP, confirm Saved; verify retries if the connection fails.
2. MEMORY: Active list includes the new note; open detail, BACK, switch Archived,
   and LOAD MORE if available.
3. ASK: type `Gdzie mieszka mój zielony smok?`; check answer and citation.
4. ASK BY VOICE: say the same question, STOP QUESTION; check transcript, Searching,
   answer and citation. Test silence separately: No speech, no answer.
5. Deny microphone permission: both voice starts disabled; text/history still work.
6. Switch ASK → MEMORY → ASK: the previous answer remains visible.

There is no arbitrary-language TTS, edit/archive/restore/delete, persisted assistant history,
multi-turn conversation, partial STT, LAN/WSS/pairing or public-storage audio.
Physical microphone/permission/device behavior requires the manual smoke;
automated tests use synthetic data and fake capture plus real OkHttp loopback
connections. They never ask against private main Memory.
