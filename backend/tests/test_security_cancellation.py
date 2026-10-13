"""Disconnects during asynchronous authentication and admitted private work."""
import asyncio
import json
from uuid import uuid4

import pytest

from backend.tests.assistant_transport_fakes import AsgiSocket
from backend.tests.test_security_core import credentials
from backend.tests.test_security_transport import Runtime, no_private_work
from backend.tests.test_server_assistant_runtime import Harness
from backend.tests.test_server_transport import start, stop
from backend.tests.test_memory_ws import _start_message, _stop_message
from kulai_memory.app_factory import create_app
from kulai_memory.security import Authenticator, SecurityConfig


def scope(path, headers, *, websocket=False):
    return dict(type="websocket" if websocket else "http", asgi={"version": "3.0"},
                scheme="ws" if websocket else "http", http_version="1.1", method="POST",
                path=path, raw_path=path.encode(), root_path="", query_string=b"",
                headers=[(b"host", b"127.0.0.1:8000"), *headers],
                server=("127.0.0.1", 8000), client=("127.0.0.1", 1234), subprotocols=[])


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("termination", ["disconnect", "cancel", "timeout", "oversized"])
def test_authentication_cancellation_drains_reader_and_never_accepts(transport, termination):
    async def scenario():
        clock, store, _, _, credential = await credentials()
        entered, drained = asyncio.Event(), asyncio.Event()
        class SlowReader:
            async def lookup(self, digest):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
        runtime = Runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, security_config=SecurityConfig(
            authentication_timeout=0.05 if termination == "timeout" else 2),
            authenticator=Authenticator(SlowReader(), clock=lambda: clock[0]))
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        ws = transport == "websocket"
        path = "/ws/memory" if ws else "/api/assistant/ask"
        headers = [(b"authorization", ("Bearer " + credential.access_token).encode())]
        await incoming.put({"type": "websocket.connect"} if ws else
                           {"type": "http.request", "body": b'{"query":"Synthetic"}', "more_body": False})
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(scope(path, headers, websocket=ws), incoming.get, outgoing.put))
            await entered.wait()
            if termination == "disconnect":
                await incoming.put({"type": "websocket.disconnect", "code": 1000} if ws else {"type": "http.disconnect"})
            elif termination == "cancel":
                task.cancel()
            elif termination == "oversized":
                await incoming.put({"type": "websocket.receive", "bytes": b"x" * 65537} if ws else
                                   {"type": "http.request", "body": b"x" * 65537, "more_body": True})
            if termination == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await asyncio.wait_for(task, 3)
            assert drained.is_set()
            messages = list(outgoing._queue)
            assert not any(m["type"] == "websocket.accept" or m.get("status") == 200 for m in messages)
            if termination in ("disconnect", "cancel"):
                assert messages == []
            else:
                assert messages[0]["type"] == ("websocket.close" if ws else "http.response.start")
        no_private_work(runtime)
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_http_body_buffer_is_replayed_exactly_after_slow_authentication():
    async def scenario():
        clock, store, _, _, credential = await credentials()
        entered, release = asyncio.Event(), asyncio.Event()
        class Reader:
            async def lookup(self, digest):
                entered.set()
                await release.wait()
                return await store.lookup(digest)
        runtime = Runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(Reader(), clock=lambda: clock[0]))
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        body = json.dumps({"query": "Synthetic UTF-8 żółw"}, ensure_ascii=False).encode()
        await incoming.put({"type": "http.request", "body": body[:10], "more_body": True})
        await incoming.put({"type": "http.request", "body": body[10:], "more_body": False})
        headers = [(b"authorization", ("Bearer " + credential.access_token).encode()), (b"content-type", b"application/json")]
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(scope("/api/assistant/ask", headers), incoming.get, outgoing.put))
            await entered.wait()
            release.set()
            await asyncio.wait_for(task, 2)
        assert runtime.rag.requests == [("Synthetic UTF-8 żółw", 5)]
        assert list(outgoing._queue)[0]["status"] == 200
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("path", ["/ws/memory", "/ws/assistant"])
@pytest.mark.parametrize("stage", ["recording", "whisper", "rag"])
def test_secure_websocket_disconnect_keeps_provider_and_wav_lifetime(monkeypatch, path, stage):
    async def scenario():
        clock, store, _, _, credential = await credentials()
        h = Harness()
        entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = h.whisper.transcribe
        async def transcribe(request):
            entered.set()
            await release.wait()
            assert request.audio.path.exists()
            drained.set()
            return await original(request)
        async def rag():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        if stage == "whisper":
            h.whisper.transcribe = transcribe
        elif stage == "rag":
            if path == "/ws/memory":
                async def indexing(**kwargs):
                    await rag()
                h.indexer.ensure = indexing
            else:
                h.rag.before = rag
        runtime = h.runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(store, clock=lambda: clock[0]))
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        headers = [(b"authorization", ("Bearer " + credential.access_token).encode())]
        await incoming.put({"type": "websocket.connect"})
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(scope(path, headers, websocket=True), incoming.get, outgoing.put))
            assert (await outgoing.get())["type"] == "websocket.accept"
            command = _start_message(uuid4()) if path == "/ws/memory" else start()
            await incoming.put({"type": "websocket.receive", "text": json.dumps(command)})
            assert json.loads((await outgoing.get())["text"])["type"] == "session.ready"
            await incoming.put({"type": "websocket.receive", "bytes": b"\0\0" * 10})
            if stage != "recording":
                command = _stop_message() if path == "/ws/memory" else stop()
                await incoming.put({"type": "websocket.receive", "text": json.dumps(command)})
                await entered.wait()
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            if stage == "whisper":
                await asyncio.get_running_loop().run_in_executor(None, lambda: None)
                assert not task.done()
                release.set()
            await asyncio.wait_for(task, 2)
            assert not runtime._ai_callers and not runtime._inference_tasks
            if stage != "recording":
                assert drained.is_set()
            events = [json.loads(m["text"])["type"] for m in outgoing._queue if m["type"] == "websocket.send"]
            assert "memory.saved" not in events and "assistant.completed" not in events
            # An already committed note can remain durable on indexing disconnect.
            if stage != "rag" or path != "/ws/memory":
                assert h.repository.create_or_get_calls == []
        assert h.rag.closed == h.bge.closed == h.engine.dispose_count == 1
    asyncio.run(asyncio.wait_for(scenario(), 10))
