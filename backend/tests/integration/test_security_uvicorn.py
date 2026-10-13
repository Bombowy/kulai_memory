"""Real HTTP/WS TCP security on loopback; synthetic runtime, no DB/migrations/AI."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from backend.tests.test_security_core import credentials
from backend.tests.test_security_transport import HTTP, WS, Runtime, no_private_work
from backend.tests.test_memory_ws import _start_message, _stop_message
from backend.tests.test_server_transport import start, stop
from kulai_memory.app_factory import create_app
from kulai_memory.security import Authenticator


@asynccontextmanager
async def server(app):
    ready = asyncio.Event()
    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        host = Server(uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False,
            access_log=False, log_level="warning", timeout_graceful_shutdown=3))
        task = asyncio.create_task(host.serve(sockets=[listener]))
        await asyncio.wait_for(ready.wait(), 5)
        try:
            yield port
        finally:
            host.should_exit = True
            await asyncio.wait_for(task, 10)


def test_real_tcp_denials_valid_rest_ws_rotation_and_secret_free_logs(capsys):
    async def scenario():
        clock, store, manager, device, credential = await credentials()
        runtime = Runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(store, clock=lambda: clock[0]))
        headers = {"Authorization": "Bearer " + credential.access_token, "X-Kulai-Device-ID": str(device.id)}
        async with server(app) as port:
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(base_url=base, timeout=5, follow_redirects=False) as client:
                assert (await client.get("/health")).json() == {"status": "ok"}
                revoked = await manager.issue(device.id)
                await manager.revoke(revoked.token_id)
                invalid_headers = [{}, {"Authorization": "Bearer " + "x" * 43},
                    {"Authorization": "Bearer " + revoked.access_token},
                    {**headers, "X-Kulai-Device-ID": str(uuid4())}, {"Authorization": "Bearer invalid"},
                    [("Authorization", headers["Authorization"]), ("Authorization", headers["Authorization"])]]
                for bad in invalid_headers:
                    for method, path in HTTP:
                        response = await client.request(method, path, headers=bad, content=b"INVALID_PRIVATE_BODY")
                        assert response.status_code == 401 and response.json() == {"detail": "Authentication required."}
                    for path in WS:
                        with pytest.raises(InvalidStatus) as caught:
                            async with connect(f"ws://127.0.0.1:{port}{path}", additional_headers=bad):
                                pytest.fail("Unauthorized WebSocket accepted.")
                        assert caught.value.response.status_code == 403
                for path in WS:
                    with pytest.raises(InvalidStatus):
                        async with connect(f"ws://127.0.0.1:{port}{path}?access_token={credential.access_token}", additional_headers=headers):
                            pytest.fail()
                no_private_work(runtime)
                assert (await client.get("/api/memories", headers=headers)).status_code == 200
                assert (await client.get(HTTP[1][1], headers=headers)).status_code == 404
                assert (await client.post("/api/assistant/ask", headers=headers, json={"query": "Synthetic"})).status_code == 200
                assert (await client.post("/api/assistant/speech-plan", headers=headers,
                                          json={"answer": "Synthetic answer"})).status_code == 200
                for path in WS:
                    async with connect(f"ws://127.0.0.1:{port}{path}", additional_headers=headers) as ws:
                        await ws.send(json.dumps(_start_message(uuid4()) if path == WS[0] else start()))
                        assert json.loads(await ws.recv())["type"] == "session.ready"
                        await ws.send(b"\0\0" * 10)
                        await ws.send(json.dumps(_stop_message() if path == WS[0] else stop()))
                        events = [json.loads(await ws.recv()) for _ in range(3)]
                        assert [e["sequence"] for e in events] == [2, 3, 4]
                        assert events[-1]["type"] == ("memory.saved" if path == WS[0] else "assistant.completed")
                successor = await manager.rotate(credential.access_token, device_id=device.id)
                assert (await client.get("/api/memories", headers=headers)).status_code == 401
                headers["Authorization"] = "Bearer " + successor.access_token
                assert (await client.get("/api/memories", headers=headers)).status_code == 200
        assert runtime.startup_count == runtime.shutdown_count == 1
        output = capsys.readouterr()
        assert credential.access_token not in output.out + output.err
        assert successor.access_token not in output.out + output.err
    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize("websocket", [False, True])
def test_real_tcp_disconnect_during_authentication_cancels_storage_before_accept(websocket):
    async def scenario():
        clock, store, _, _, credential = await credentials()
        entered, drained = asyncio.Event(), asyncio.Event()
        class Reader:
            async def lookup(self, digest):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
        runtime = Runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(Reader(), clock=lambda: clock[0]))
        async with server(app) as port:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            target = "/ws/memory" if websocket else "/api/memories"
            request = f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {credential.access_token}\r\n"
            if websocket:
                request += "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            writer.write((request + "\r\n").encode())
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 3)
            writer.close(); await writer.wait_closed()
            await asyncio.wait_for(drained.wait(), 3)
        no_private_work(runtime)
    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_real_tcp_secure_disconnect_during_ask_cancels_operation_then_next_request_works():
    async def scenario():
        clock, store, _, _, credential = await credentials()
        entered, drained = asyncio.Event(), asyncio.Event()
        runtime = Runtime()
        original = runtime.ask_memory
        first = True
        async def ask(**kwargs):
            nonlocal first
            if first:
                first = False
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
            return await original(**kwargs)
        runtime.ask_memory = ask
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(store, clock=lambda: clock[0]))
        async with server(app) as port:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            body = b'{"query":"Synthetic"}'
            request = (f"POST /api/assistant/ask HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       f"Authorization: Bearer {credential.access_token}\r\nContent-Type: application/json\r\n"
                       f"Content-Length: {len(body)}\r\n\r\n").encode() + body
            writer.write(request); await writer.drain()
            await asyncio.wait_for(entered.wait(), 3)
            writer.close(); await writer.wait_closed()
            await asyncio.wait_for(drained.wait(), 3)
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(f"http://127.0.0.1:{port}/api/assistant/ask",
                    headers={"Authorization": "Bearer " + credential.access_token}, json={"query": "Synthetic next"})
                assert response.status_code == 200
            assert runtime.rag.requests == [("Synthetic next", 5)]
        assert runtime.shutdown_count == 1
    asyncio.run(asyncio.wait_for(scenario(), 15))
