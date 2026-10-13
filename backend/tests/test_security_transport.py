"""All private HTTP/WS routes; no private runtime operation after a denial."""
import asyncio
import json
import logging
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from backend.tests.local_transport import TestClient
from backend.tests.test_security_core import credentials
from backend.tests.test_server_speech import SpeechTransportRuntime
from backend.tests.test_memory_ws import _start_message, _stop_message
from backend.tests.test_server_transport import start, stop
from kulai_memory.app_factory import create_app
from kulai_memory.security import Action, Authenticator, SecurityConfig, SecurityMode

HTTP = [("GET", "/api/memories"), ("GET", "/api/memories/" + str(UUID(int=1))),
        ("POST", "/api/assistant/ask"), ("POST", "/api/assistant/speech-plan")]
WS = ["/ws/memory", "/ws/assistant"]


class Runtime(SpeechTransportRuntime):
    def __init__(self):
        super().__init__([("Synthetic answer", "pl")])
        self.private_calls = []
        for name in ("list_memories", "get_memory", "ask_memory", "plan_speech", "transcribe_question",
                     "transcribe", "ingest"):
            original = getattr(self, name)
            async def tracked(_name=name, _operation=original, **kwargs):
                self.private_calls.append(_name)
                return await _operation(**kwargs)
            setattr(self, name, tracked)


async def setup(case="valid", *, mode=SecurityMode.SECURE, reader=None):
    clock, store, manager, device, credential = await credentials()
    runtime = Runtime()
    headers = {"Authorization": "Bearer " + credential.access_token, "X-Kulai-Device-ID": str(device.id)}
    if case == "missing":
        headers = {}
    elif case == "invalid":
        headers["Authorization"] = "Bearer " + "x" * 43
    elif case == "expired":
        clock[0] = credential.expires_at
    elif case == "revoked":
        await manager.revoke(credential.token_id)
    elif case == "revoked_device":
        await manager.revoke_device(device.id)
    elif case == "other_device":
        headers["X-Kulai-Device-ID"] = str((await manager.register_device(trusted=True)).id)
    elif case == "unconfigured":
        store = None
    auth = Authenticator(reader or store, clock=lambda: clock[0]) if reader or store else None
    app = create_app(voice_runtime_factory=lambda: runtime, security_config=SecurityConfig(mode=mode), authenticator=auth)
    return app, runtime, headers, manager, credential


def no_private_work(runtime):
    assert runtime.private_calls == runtime.session_ids == runtime.ingest_calls == []
    assert runtime.sessions.sessions == runtime.rag.requests == runtime.paths == runtime.answers == []
    assert runtime.provider.call_count == 0


@pytest.mark.parametrize("case", ["missing", "invalid", "expired", "revoked", "revoked_device", "other_device", "unconfigured"])
@pytest.mark.parametrize("route", HTTP + [("WS", p) for p in WS])
def test_all_private_routes_deny_without_ai_data_audio_or_session(case, route, monkeypatch):
    app, runtime, headers, _, _ = asyncio.run(setup(case))
    from kulai_memory.api import memory_ws, assistant_ws
    def forbidden_wav():
        pytest.fail("Unauthorized operation allocated audio.")
    monkeypatch.setattr(memory_ws, "OwnedPcmWav", forbidden_wav)
    monkeypatch.setattr(assistant_ws, "OwnedPcmWav", forbidden_wav)
    with TestClient(app) as client:
        if route[0] == "WS":
            with pytest.raises(WebSocketDisconnect) as caught:
                with client.websocket_connect(route[1], headers=headers):
                    pytest.fail("Unauthorized WebSocket was accepted.")
            assert caught.value.code == 1008
        else:
            response = client.request(*route, headers=headers, content=b"PRIVATE_MEMORY_INVALID_BODY")
            assert response.status_code == 401
            assert response.json() == {"detail": "Authentication required."}
            assert response.headers["WWW-Authenticate"] == "Bearer"
            assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/health").json() == {"status": "ok"}
    no_private_work(runtime)


def test_valid_token_all_rest_and_ws_contracts_and_no_secret_diagnostics(caplog):
    app, runtime, headers, _, credential = asyncio.run(setup())
    handler = logging.getLogger("kulai_memory.api.authentication")
    handler.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            private_headers = {**headers, "X-Request-ID": credential.access_token, "User-Agent": credential.access_token}
            assert client.get("/api/memories", headers=private_headers).json() == {"items": [], "next_cursor": None}
            assert client.get(HTTP[1][1], headers=headers).status_code == 404
            answer = client.post("/api/assistant/ask", headers=headers, json={"query": "Synthetic"})
            assert answer.status_code == 200 and set(answer.json()) == {"status", "answer", "citations"}
            assert client.post("/api/assistant/speech-plan", headers=headers, json={"answer": "Synthetic answer"}).json() == {
                "segments": [{"text": "Synthetic answer", "language": "pl"}]}
            for path in WS:
                with client.websocket_connect(path, headers=headers) as ws:
                    ws.send_json(_start_message(uuid4()) if path == WS[0] else start())
                    assert ws.receive_json()["type"] == "session.ready"
                    ws.send_bytes(b"\0\0" * 10)
                    ws.send_json(_stop_message() if path == WS[0] else stop())
                    events = [ws.receive_json() for _ in range(3)]
                    assert [e["type"] for e in events] == (["transcript.final", "memory.saving", "memory.saved"]
                        if path == WS[0] else ["transcript.final", "rag.started", "assistant.completed"])
                    assert [e["sequence"] for e in events] == [2, 3, 4]
            assert "x-request-id" not in answer.headers
    finally:
        handler.removeHandler(caplog.handler)
    assert credential.access_token not in caplog.text
    assert credential.access_token not in str([r.__dict__ for r in caplog.records])
    assert runtime.startup_count == runtime.shutdown_count == 1


@pytest.mark.parametrize("header", ["", "Basic password", "Bearer", "Bearer ", "Bearer  " + "x" * 43,
    "Bearer\t" + "x" * 43, "Bearer " + "x" * 43 + " ", "Bearer " + "x" * 43 + ",Bearer x",
    "Bearer " + "x" * 42, "Bearer " + "x" * 44])
@pytest.mark.parametrize("path", ["/api/memories", *WS])
def test_malformed_headers_deny_all_transports(header, path):
    app, runtime, _, _, _ = asyncio.run(setup())
    with TestClient(app) as client:
        if path.startswith("/ws"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(path, headers={"Authorization": header}):
                    pytest.fail()
        else:
            assert client.get(path, headers={"Authorization": header}).status_code == 401
    no_private_work(runtime)


@pytest.mark.parametrize("attack", ["duplicate_auth", "device_format", "project_format", "origin", "proxy",
    "query_token", "query_encoded", "cookie", "subprotocol", "host", "duplicate_host", "new_route", "trailing_slash"])
def test_bypass_attempts_are_rejected_before_private_operations(attack):
    app, runtime, headers, _, credential = asyncio.run(setup())
    path = "/api/memories"
    if attack == "duplicate_auth":
        headers = [("authorization", headers["Authorization"]), ("Authorization", headers["Authorization"])]
    elif attack == "device_format":
        headers["X-Kulai-Device-ID"] = "invalid"
    elif attack == "project_format":
        headers["X-Kulai-Project-ID"] = "invalid"
    elif attack == "origin":
        headers["Origin"] = "http://evil.invalid"
    elif attack == "proxy":
        headers["X-Forwarded-For"] = "127.0.0.1"
    elif attack in ("query_token", "query_encoded"):
        path += "?" + ("access_token" if attack == "query_token" else "%61ccess_token") + "=" + credential.access_token
    elif attack == "cookie":
        headers = {"Cookie": "access_token=" + credential.access_token}
    elif attack == "subprotocol":
        headers["Sec-WebSocket-Protocol"] = credential.access_token
    elif attack == "host":
        headers["Host"] = "evil.invalid"
    elif attack == "duplicate_host":
        headers = [*headers.items(), ("Host", "127.0.0.1"), ("host", "evil.invalid")]
    elif attack == "trailing_slash":
        headers = {}
        path += "/"
    else:
        @app.get("/future/private")
        async def future():
            runtime.private_calls.append("future")
            return {"data": "private"}
        path = "/future/private"
    with TestClient(app) as client:
        response = client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code in (401, 403)
        assert credential.access_token not in response.text
    no_private_work(runtime)


def test_authorization_action_and_project_boundaries():
    async def scenario():
        clock, store, manager, device, _ = await credentials()
        credential = await manager.issue(device.id, actions=frozenset({Action.MEMORY_READ}), project_id=UUID(int=7))
        runtime = Runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(store, clock=lambda: clock[0]))
        return app, runtime, credential
    app, runtime, credential = asyncio.run(scenario())
    headers = {"Authorization": "Bearer " + credential.access_token}
    with TestClient(app) as client:
        assert client.get("/api/memories", headers=headers).status_code == 403
        headers["X-Kulai-Project-ID"] = str(UUID(int=8))
        assert client.get("/api/memories", headers=headers).status_code == 403
        headers["X-Kulai-Project-ID"] = str(UUID(int=7))
        assert client.post("/api/assistant/ask", headers=headers, json={"query": "Synthetic"}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/memory", headers=headers):
                pytest.fail()
        no_private_work(runtime)
        assert client.get("/api/memories", headers=headers).status_code == 403
        no_private_work(runtime)


def test_local_dev_never_downgrades_bad_credentials_and_enforces_loopback():
    app, runtime, _, _, _ = asyncio.run(setup("missing", mode=SecurityMode.LOCAL_DEV))
    with TestClient(app) as client:
        assert client.get("/api/memories").status_code == 200
        assert client.get("/api/memories", headers={"Authorization": "invalid"}).status_code == 401
        assert client.get("/api/memories", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    from fastapi.testclient import TestClient as RawClient
    for host, peer in [("127.0.0.1", "192.168.1.9"), ("192.168.1.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1")]:
        app, runtime, _, _, _ = asyncio.run(setup("missing", mode=SecurityMode.LOCAL_DEV))
        with RawClient(app, base_url="http://" + host, client=(peer, 10)) as client:
            assert client.get("/api/memories").status_code in (401, 403)
        no_private_work(runtime)


def test_store_failure_is_safe_and_does_not_fall_back(caplog):
    class BrokenReader:
        async def lookup(self, digest):
            raise RuntimeError("PRIVATE_DB_SECRET_SENTINEL")
    app, runtime, headers, _, _ = asyncio.run(setup(reader=BrokenReader(), mode=SecurityMode.LOCAL_DEV))
    with TestClient(app) as client:
        result = client.get("/api/memories", headers=headers)
        assert result.status_code == 401
        assert "PRIVATE" not in result.text + caplog.text
    no_private_work(runtime)


@pytest.mark.parametrize("path", WS)
def test_revocation_on_open_websocket_rejects_next_private_frame(path):
    app, runtime, headers, manager, credential = asyncio.run(setup())
    with TestClient(app) as client:
        with client.websocket_connect(path, headers=headers) as ws:
            asyncio.run(manager.revoke(credential.token_id))
            ws.send_json(_start_message(uuid4()) if path == WS[0] else start())
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_json()
            assert caught.value.code == 1008
    no_private_work(runtime)


@pytest.mark.parametrize("path", WS)
def test_expiry_on_open_websocket_rejects_next_private_frame(path):
    async def scenario():
        clock, store, _, _, credential = await credentials()
        runtime = Runtime()
        app = create_app(voice_runtime_factory=lambda: runtime, authenticator=Authenticator(store, clock=lambda: clock[0]))
        return app, runtime, clock, credential
    app, runtime, clock, credential = asyncio.run(scenario())
    with TestClient(app) as client:
        with client.websocket_connect(path, headers={"Authorization": "Bearer " + credential.access_token}) as ws:
            clock[0] = credential.expires_at
            ws.send_json(_start_message(uuid4()) if path == WS[0] else start())
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_json()
            assert caught.value.code == 1008
    no_private_work(runtime)


def test_unhandled_adapter_error_is_safe_and_not_rethrown_to_server(caplog):
    app, runtime, headers, _, credential = asyncio.run(setup())
    # Simulate a transport failure outside _SafeRoute; gate remains authoritative.
    app.router.routes.clear()
    @app.get("/api/memories")
    async def unexpected():
        raise RuntimeError("PRIVATE_ADAPTER_SECRET " + credential.access_token)
    with TestClient(app) as client:
        response = client.get("/api/memories", headers=headers)
        assert response.status_code == 500
        assert response.json() == {"detail": "Operation could not be completed."}
    assert "PRIVATE_ADAPTER_SECRET" not in caplog.text
    assert credential.access_token not in response.text + caplog.text


def test_error_code_outside_allowlist_cannot_leak_credential(caplog):
    from kulai_memory.application.rag import MemoryRagError
    app, runtime, headers, _, credential = asyncio.run(setup())
    runtime.rag.error = MemoryRagError(credential.access_token)
    handler = logging.getLogger("kulai_memory.api.memory_http")
    handler.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            response = client.post("/api/assistant/ask", headers=headers, json={"query": "Synthetic"})
            assert response.status_code == 500
    finally:
        handler.removeHandler(caplog.handler)
    assert credential.access_token not in caplog.text + response.text
