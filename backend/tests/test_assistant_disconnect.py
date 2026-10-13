"""Actual ASGI disconnects through the production server AI cancellation lane."""
import asyncio
import json

import pytest

from backend.tests.assistant_transport_fakes import AsgiSocket
from backend.tests.test_server_assistant_runtime import Harness
from backend.tests.test_server_transport import start, stop
from kulai_memory.api import assistant_ws
from backend.tests.local_transport import create_local_app as create_app


@pytest.mark.parametrize("stage", ["recording", "whisper", "rag", "invalid_during_work"])
def test_assistant_disconnect_cleanup_and_no_stale_result(monkeypatch, stage):
    async def scenario():
        h = Harness()
        paths = []
        original_wav = assistant_ws.OwnedPcmWav
        def wav():
            result = original_wav()
            paths.append(result.path)
            return result
        monkeypatch.setattr(assistant_ws, "OwnedPcmWav", wav)
        entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_stt = h.whisper.transcribe
        async def transcribe(request):
            entered.set()
            await release.wait()
            assert request.audio.path.exists()
            drained.set()
            return await original_stt(request)
        async def generate():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        if stage == "whisper":
            h.whisper.transcribe = transcribe
        elif stage in ("rag", "invalid_during_work"):
            h.rag.before = generate
        runtime = h.runtime()
        app = create_app(voice_runtime_factory=lambda: runtime)
        async with app.router.lifespan_context(app):
            ws = await AsgiSocket(app).connect()
            await ws.send_json(start())
            assert (await ws.event())["type"] == "session.ready"
            assert paths[0].exists()
            if stage != "recording":
                await ws.send_bytes(b"\0\0" * 100)
                await ws.send_json(stop())
                await entered.wait()
                if stage != "whisper":
                    assert (await ws.event())["type"] == "transcript.final"
                    assert (await ws.event())["type"] == "rag.started"
                    assert not paths[0].exists()
            if stage == "invalid_during_work":
                await ws.send_bytes(b"\0\0")
                assert (await ws.event())["type"] == "error"
                await ws.closed(1002)
            else:
                await ws.disconnect()
                if stage == "whisper":
                    # Wait for the cancellation to reach the shield/drain boundary.
                    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
                    assert paths[0].exists() and not ws.task.done()
                    release.set()
                await asyncio.wait_for(ws.task, 2)
            assert not paths[0].exists() and ws.outgoing.empty()
            assert h.repository.create_or_get_calls == h.indexer.calls == []
            assert not runtime._inference_tasks and not runtime._ai_callers
            if stage != "recording":
                assert drained.is_set()
        assert h.rag.closed == h.bge.closed == h.engine.dispose_count == 1
    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_http_disconnect_cancels_canonical_request_and_does_not_leak_logs(caplog):
    async def scenario():
        h, entered, cancelled = Harness(), asyncio.Event(), asyncio.Event()
        async def model():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        h.rag.before = model
        runtime = h.runtime()
        app = create_app(voice_runtime_factory=lambda: runtime)
        async with app.router.lifespan_context(app):
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            body = json.dumps({"query": "PRIVATE_QUERY_SENTINEL"}).encode()
            await incoming.put(dict(type="http.request", body=body, more_body=False))
            scope = dict(type="http", asgi={"version": "3.0"}, method="POST", scheme="http",
                         http_version="1.1", path="/api/assistant/ask", raw_path=b"/api/assistant/ask",
                         root_path="", query_string=b"", headers=[(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
                         server=("127.0.0.1", 8000), client=("127.0.0.1", 1234))
            task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
            await entered.wait()
            await incoming.put(dict(type="http.disconnect"))
            # Core HTTP middleware may suppress client-disconnect cancellation.
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert cancelled.is_set() and not runtime._ai_callers and not h.sessions.sessions
            assert not any("PRIVATE_QUERY_SENTINEL" in r.getMessage() for r in caplog.records)
    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_disconnect_after_transcript_is_observed_before_rag_starts():
    async def scenario():
        h = Harness()
        runtime = h.runtime()
        app = create_app(voice_runtime_factory=lambda: runtime)
        ws = None
        async def send(message):
            if message["type"] == "websocket.send" and json.loads(message["text"])["type"] == "transcript.final":
                await ws.disconnect()
                # The actual ASGI send is blocked; the continuous receive watcher
                # must cancel it rather than let RAG or completed events start.
                await asyncio.Event().wait()
        async with app.router.lifespan_context(app):
            ws = await AsgiSocket(app, send_hook=send).connect()
            await ws.send_json(start())
            await ws.event()
            await ws.send_bytes(b"\0\0" * 100)
            await ws.send_json(stop())
            assert (await ws.event())["type"] == "transcript.final"
            await asyncio.wait_for(ws.task, 2)
            assert not h.rag.requests and ws.outgoing.empty()
            assert not runtime._ai_callers
    asyncio.run(asyncio.wait_for(scenario(), 10))
