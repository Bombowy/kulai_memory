"""Actual HTTP/WS TCP, managed SECURE and synthetic marker-owned PostgreSQL 0005."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from uuid import uuid4

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from backend.tests.integration.test_conversation_service_postgres import running_conversations
from backend.tests.integration.test_security_uvicorn import server
from backend.tests.integration.test_sessions_postgres import paired, age_refresh
from backend.tests.test_server_transport import start, stop
from backend.tests.remote_tls_fixtures import certificates, tls_server
from backend.tests.integration.test_remote_ingress_tls import application, client, websocket
from kulai_memory.app_factory import create_app
from kulai_memory.database_safety import database_snapshot_url
from kulai_memory.security import Action, CredentialManager, SecurityConfig, SecurityMode
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime


def auth(credential):
    return {"Authorization": "Bearer " + credential.access_token, "X-Kulai-Device-ID": str(credential.device_id)}


def question(text="Pytanie syntetyczne", request_id=None):
    return dict(question=text, request_id=str(request_id or uuid4()), input_kind="text")


@asynccontextmanager
async def api(monkeypatch):
    async with running_conversations(monkeypatch, security=True) as h:
        h.store, _, _, _, h.credential = await paired(h.factory, actions=frozenset(Action))
        h.headers = auth(h.credential)
        h.app = create_app(voice_runtime_factory=lambda: h.runtime,
            security_config=SecurityConfig(mode=SecurityMode.SECURE),
            security_runtime_factory=lambda: PostgresSecurityRuntime(config_factory=lambda: h.target))
        async with server(h.app) as port:
            h.port, h.url, h.ws_url = port, f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws/assistant"
            async with httpx.AsyncClient(base_url=h.url, timeout=10, follow_redirects=False) as client:
                h.client = client
                yield h


async def create(h, title="Rozmowa testowa"):
    response = await h.client.post("/api/conversations", headers=h.headers, json={"title": title})
    assert response.status_code == 201
    assert "no-store" in response.headers["cache-control"]
    return response.json()["conversation_id"]


async def voice(h, conversation_id, request_id, *, pcm=b"\0\0" * 100, headers=None):
    async with connect(h.ws_url, additional_headers=headers or h.headers, proxy=None) as ws:
        await ws.send(json.dumps({**start(), "conversation_id": conversation_id, "request_id": str(request_id)}))
        events = [json.loads(await ws.recv())]
        if pcm:
            await ws.send(pcm)
        await ws.send(json.dumps(stop()))
        events.extend([json.loads(await ws.recv()) for _ in range(3)])
        await ws.wait_closed()
        assert ws.close_code == 1000
        assert [event["type"] for event in events] == ["session.ready", "transcript.final", "rag.started", "conversation.completed"]
        assert [event["sequence"] for event in events] == [1, 2, 3, 4]
        return events[-1]["payload"]


def test_text_voice_history_resume_pagination_and_exact_request_replay(monkeypatch):
    async def run():
        async with api(monkeypatch) as h:
            first, other = await create(h), await create(h, "Oddzielna rozmowa")
            path = f"/api/conversations/{first}"
            body = question("Projekt: notesy A5 po 30 zł.")
            response = await h.client.post(path + "/ask", headers=h.headers, json=body)
            assert response.status_code == 200
            text = response.json()
            assert text["created"] and text["turn"]["turn_number"] == 1
            assert text["turn"]["user"]["input_kind"] == "text"
            assert text["turn"]["request_id"] == body["request_id"]
            h.runtime._provider._texts = ("A jak to promować?",)
            voice_id = uuid4()
            spoken = await voice(h, first, voice_id)
            assert spoken["turn"]["conversation_id"] == first and spoken["turn"]["turn_number"] == 2
            assert spoken["turn"]["user"]["input_kind"] == "voice"
            assert spoken["turn"]["user"]["content"] == "A jak to promować?"
            assert h.llm.calls[-1].messages[1].content == body["question"]
            assert all(not request.audio.path.exists() for request in h.runtime._provider.requests)
            counts = h.llm.attempts, len(h.runtime._provider.requests)
            replay = await voice(h, first, voice_id, pcm=b"")
            assert not replay["created"] and replay["turn"] == spoken["turn"]
            assert (h.llm.attempts, len(h.runtime._provider.requests)) == counts
            # New client/socket and physical PostgreSQL connections retain the conversation.
            await h.factory.kw["bind"].dispose()
            async with httpx.AsyncClient(base_url=h.url, timeout=10) as reconnected:
                continued = await reconnected.post(path + "/ask", headers=h.headers, json=question("Rozwiń poprzednią odpowiedź."))
                assert continued.status_code == 200 and continued.json()["turn"]["turn_number"] == 3
            saved = await h.client.post(path + "/ask", headers=h.headers, json=body)
            assert not saved.json()["created"] and saved.json()["turn"] == text["turn"]
            assert h.llm.attempts == 3
            # History windows are chronological and bounded, with no cross-talk.
            latest = (await h.client.get(path + "/messages", headers=h.headers, params={"limit": 2})).json()
            older = (await h.client.get(path + "/messages", headers=h.headers,
                params={"limit": 2, "before_sequence": latest["next_before_sequence"]})).json()
            assert [m["sequence"] for m in latest["messages"]] == [5, 6]
            assert [m["sequence"] for m in older["messages"]] == [3, 4]
            assert (await h.client.get(f"/api/conversations/{other}/messages", headers=h.headers)).json()["messages"] == []
            page = (await h.client.get("/api/conversations", headers=h.headers, params={"limit": 1})).json()
            next_page = (await h.client.get("/api/conversations", headers=h.headers,
                params={"limit": 1, **page["next_cursor"]})).json()
            assert {page["conversations"][0]["conversation_id"], next_page["conversations"][0]["conversation_id"]} == {first, other}
            assert next_page["next_cursor"] is None
            before = await database_snapshot_url(h.target.async_url)
            assert before.memories.count == before.vectors.count == 0
            assert before.conversations.turns.count == 3 and before.conversations.messages.count == 6
    asyncio.run(asyncio.wait_for(run(), 120))


def test_secure_authorization_and_refresh_use_existing_paired_credentials(monkeypatch):
    async def run():
        async with api(monkeypatch) as h:
            identity = await create(h)
            root = f"/api/conversations/{identity}"
            routes = [("POST", "/api/conversations"), ("GET", "/api/conversations"),
                      ("GET", root + "/messages"), ("POST", root + "/ask")]
            before = await database_snapshot_url(h.target.async_url)
            for bad in ({}, {"Authorization": "Bearer " + "x" * 43}):
                for method, path in routes:
                    response = await h.client.request(method, path, headers=bad, content=b"PRIVATE_BAD_BODY")
                    assert response.status_code == 401 and "PRIVATE" not in response.text
                with pytest.raises(InvalidStatus) as denied:
                    async with connect(h.ws_url, additional_headers=bad, proxy=None): pytest.fail("Anonymous WS accepted")
                assert denied.value.response.status_code == 403
            assert await database_snapshot_url(h.target.async_url) == before
            manager = CredentialManager(h.store)
            reader = await manager.issue(h.credential.device_id, actions=frozenset({Action.MEMORY_READ}))
            asker = await manager.issue(h.credential.device_id, actions=frozenset({Action.ASSISTANT_ASK}))
            unrelated = await manager.issue(h.credential.device_id, actions=frozenset({Action.MEMORY_WRITE}))
            for method, path in routes:
                if method == "GET":
                    assert (await h.client.request(method, path, headers=auth(reader))).status_code == 200
                    assert (await h.client.request(method, path, headers=auth(asker))).status_code == 403
                else:
                    assert (await h.client.request(method, path, headers=auth(reader), json=question())).status_code == 403
                assert (await h.client.request(method, path, headers=auth(unrelated), json={})).status_code == 403
            assert (await h.client.post("/api/conversations", headers=auth(asker), json={})).status_code == 201
            assert (await h.client.post(root + "/ask", headers=auth(asker), json=question())).status_code == 200
            for denied in (reader, unrelated):
                with pytest.raises(InvalidStatus):
                    async with connect(h.ws_url, additional_headers=auth(denied), proxy=None): pytest.fail("Unauthorized WS accepted")
            # The existing refresh endpoint rotates the bearer; conversation identity persists.
            await age_refresh(h.factory)
            renewed = await h.client.post("/api/security/refresh",
                headers={"Authorization": "Refresh " + h.credential.refresh_credential},
                json={key: str(getattr(h.credential, key)) for key in ("session_id", "device_id", "refresh_id")})
            assert renewed.status_code == 200
            assert (await h.client.get(root + "/messages", headers=h.headers)).status_code == 401
            fresh = renewed.json()
            fresh_headers = {"Authorization": "Bearer " + fresh["access_token"], "X-Kulai-Device-ID": fresh["device_id"]}
            assert (await h.client.get(root + "/messages", headers=fresh_headers)).status_code == 200
            await manager.revoke(asker.token_id)
            assert (await h.client.post(root + "/ask", headers=auth(asker), json=question())).status_code == 401
            with pytest.raises(InvalidStatus):
                async with connect(h.ws_url, additional_headers=auth(asker), proxy=None): pytest.fail("Revoked WS accepted")
    asyncio.run(asyncio.wait_for(run(), 120))


def test_concurrent_http_retries_create_one_turn_and_call_qwen_once(monkeypatch):
    async def run():
        async with api(monkeypatch) as h:
            identity = await create(h)
            body = question()
            replies = await asyncio.gather(*(h.client.post(f"/api/conversations/{identity}/ask",
                headers=h.headers, json=body) for _ in range(8)))
            assert all(response.status_code == 200 for response in replies)
            values = [response.json() for response in replies]
            assert sum(value["created"] for value in values) == h.llm.attempts == 1
            assert all(value["turn"] == values[0]["turn"] for value in values)
            snapshot = await database_snapshot_url(h.target.async_url)
            assert snapshot.conversations.turns.count == 1 and snapshot.conversations.messages.count == 2
    asyncio.run(asyncio.wait_for(run(), 120))


def test_validation_conflicts_missing_conversation_and_generation_failure_are_safe(monkeypatch, caplog):
    async def run():
        async with api(monkeypatch) as h:
            first, second = await create(h), await create(h)
            root = f"/api/conversations/{first}"
            for body in ({"request_id": "PRIVATE", "question": "PRIVATE"}, question(" "), question("x" * 10001),
                         {**question(), "input_kind": "audio"}, {**question(), "extra": "PRIVATE"}):
                response = await h.client.post(root + "/ask", headers=h.headers, json=body)
                assert response.status_code == 422 and "PRIVATE" not in response.text
            for params in ({"limit": 0}, {"limit": 101}, {"before_id": first},
                           {"before_activity_at": "2026-10-20T12:00:00", "before_id": first}):
                assert (await h.client.get("/api/conversations", headers=h.headers, params=params)).status_code == 422
            assert (await h.client.get(root + "/messages", headers=h.headers, params={"before_sequence": 0})).status_code == 422
            assert (await h.client.get("/api/conversations/PRIVATE/messages", headers=h.headers)).status_code == 422
            assert (await h.client.get(f"/api/conversations/{uuid4()}/messages", headers=h.headers)).status_code == 404
            missing = await h.client.post(f"/api/conversations/{uuid4()}/ask", headers=h.headers, json=question())
            assert missing.status_code == 404 and h.llm.attempts == 0
            body = question()
            assert (await h.client.post(root + "/ask", headers=h.headers, json=body)).status_code == 200
            for path, changed in ((root, {**body, "question": "Changed"}), (f"/api/conversations/{second}", body),
                                  (root, {**body, "input_kind": "voice"})):
                assert (await h.client.post(path + "/ask", headers=h.headers, json=changed)).status_code == 409
            assert h.llm.attempts == 1
            before = await database_snapshot_url(h.target.async_url)
            h.llm.error = RuntimeError("PRIVATE_GENERATION_ERROR")
            failed = await h.client.post(root + "/ask", headers=h.headers, json=question())
            assert failed.status_code == 502 and "PRIVATE" not in failed.text
            assert await database_snapshot_url(h.target.async_url) == before
            async with connect(h.ws_url, additional_headers=h.headers, proxy=None) as ws:
                await ws.send(json.dumps({**start(), "conversation_id": first, "request_id": str(uuid4())}))
                assert json.loads(await ws.recv())["type"] == "session.ready"
                await ws.send(b"\0\0" * 100)
                await ws.send(json.dumps(stop()))
                events = [json.loads(await ws.recv()) for _ in range(3)]
                assert events[-1]["payload"]["code"] == "conversation.generation_failed"
                await ws.wait_closed()
                assert ws.close_code == 1011
            assert await database_snapshot_url(h.target.async_url) == before
            assert "PRIVATE_GENERATION_ERROR" not in caplog.text
    asyncio.run(asyncio.wait_for(run(), 120))


def test_disconnect_http_and_ws_cancels_generation_without_saving_partial_turn(monkeypatch):
    async def run():
        async with api(monkeypatch) as h:
            identity = await create(h)
            before = await database_snapshot_url(h.target.async_url)
            for transport in ("http", "ws"):
                entered, drained = asyncio.Event(), asyncio.Event()
                async def blocked():
                    entered.set()
                    try: await asyncio.Event().wait()
                    finally: drained.set()
                h.llm.before = blocked
                request_id = uuid4()
                if transport == "http":
                    _, writer = await asyncio.open_connection("127.0.0.1", h.port)
                    payload = json.dumps(question(request_id=request_id)).encode()
                    head = (f"POST /api/conversations/{identity}/ask HTTP/1.1\r\nHost: 127.0.0.1:{h.port}\r\n"
                            f"Authorization: {h.headers['Authorization']}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n")
                    writer.write(head.encode() + payload)
                    await writer.drain()
                    await asyncio.wait_for(entered.wait(), 5)
                    writer.close()
                    await writer.wait_closed()
                else:
                    async with connect(h.ws_url, additional_headers=h.headers, proxy=None) as ws:
                        await ws.send(json.dumps({**start(), "conversation_id": identity, "request_id": str(request_id)}))
                        await ws.recv()
                        await ws.send(b"\0\0" * 100)
                        await ws.send(json.dumps(stop()))
                        assert [json.loads(await ws.recv())["type"] for _ in range(2)] == ["transcript.final", "rag.started"]
                        await asyncio.wait_for(entered.wait(), 5)
                        await ws.close()
                await asyncio.wait_for(drained.wait(), 5)
                # The watcher drains cancellation before a new operation acquires the same AI lane.
                await h.runtime._run_ai(lambda: asyncio.sleep(0))
                assert await database_snapshot_url(h.target.async_url) == before
                assert h.factory.kw["bind"].pool.checkedout() == 0
                assert all(not item.audio.path.exists() for item in h.runtime._provider.requests)
            h.llm.before = None
            retried = await h.client.post(f"/api/conversations/{identity}/ask", headers=h.headers,
                                          json=question(request_id=request_id))
            assert retried.status_code == 200 and retried.json()["turn"]["turn_number"] == 1
    asyncio.run(asyncio.wait_for(run(), 120))


def test_voice_identity_validation_empty_input_conflicts_and_live_revocation(monkeypatch):
    async def run():
        async with api(monkeypatch) as h:
            first, second = await create(h), await create(h)
            before = await database_snapshot_url(h.target.async_url)
            for identifiers in ({"conversation_id": first}, {"request_id": str(uuid4())},
                                {"conversation_id": first, "request_id": "PRIVATE"}):
                async with connect(h.ws_url, additional_headers=h.headers, proxy=None) as ws:
                    await ws.send(json.dumps({**start(), **identifiers}))
                    error = json.loads(await ws.recv())
                    assert error["payload"]["code"] == "protocol.invalid_message"
                    assert "PRIVATE" not in json.dumps(error)
                    await ws.wait_closed()
                    assert ws.close_code == 1002
            for pcm in (b"", b"\0\0" * 100):
                h.runtime._provider._texts = (" ",)
                async with connect(h.ws_url, additional_headers=h.headers, proxy=None) as ws:
                    await ws.send(json.dumps({**start(), "conversation_id": first, "request_id": str(uuid4())}))
                    assert json.loads(await ws.recv())["type"] == "session.ready"
                    if pcm: await ws.send(pcm)
                    await ws.send(json.dumps(stop()))
                    assert not json.loads(await ws.recv())["payload"]["text"].strip()
                    await ws.wait_closed()
                    assert ws.close_code == 1000
            assert h.llm.attempts == 0 and await database_snapshot_url(h.target.async_url) == before
            h.runtime._provider._texts = ("Pytanie głosowe",)
            spoken_id, text_id = uuid4(), uuid4()
            await voice(h, first, spoken_id)
            assert (await h.client.post(f"/api/conversations/{first}/ask", headers=h.headers,
                                        json=question(request_id=text_id))).status_code == 200
            counts = h.llm.attempts, len(h.runtime._provider.requests)
            before = await database_snapshot_url(h.target.async_url)
            for identity, request_id in ((second, spoken_id), (first, text_id)):
                async with connect(h.ws_url, additional_headers=h.headers, proxy=None) as ws:
                    await ws.send(json.dumps({**start(), "conversation_id": identity, "request_id": str(request_id)}))
                    await ws.recv()
                    await ws.send(json.dumps(stop()))
                    assert json.loads(await ws.recv())["payload"]["code"] == "conversation.request_conflict"
                    await ws.wait_closed()
                    assert ws.close_code == 1008
            assert (h.llm.attempts, len(h.runtime._provider.requests)) == counts
            assert await database_snapshot_url(h.target.async_url) == before
            async with connect(h.ws_url, additional_headers=h.headers, proxy=None) as ws:
                await ws.send(json.dumps({**start(), "conversation_id": first, "request_id": str(uuid4())}))
                await ws.recv()
                await CredentialManager(h.store).revoke(h.credential.token_id)
                await ws.send(b"\0\0" * 100)
                with pytest.raises(ConnectionClosed): await ws.recv()
                assert ws.close_code == 1008
            assert (h.llm.attempts, len(h.runtime._provider.requests)) == counts
    asyncio.run(asyncio.wait_for(run(), 120))


def test_conversations_through_real_mtls_https_wss_and_strict_ingress_allowlist(monkeypatch, certificates):
    async def run():
        async with running_conversations(monkeypatch, security=True) as h:
            _, _, _, _, credential = await paired(h.factory, actions=frozenset(Action))
            app, _ = application(h.target, h.factory, certificates.config(), runtime=h.runtime)
            async with tls_server(app) as (port, _):
                async with client(certificates, port) as transport:
                    assert (await transport.post("/api/conversations", json={})).status_code == 401
                    created = await transport.post("/api/conversations", headers=auth(credential), json={})
                    assert created.status_code == 201
                    identity = created.json()["conversation_id"]
                    root = f"/api/conversations/{identity}"
                    body = question("Synthetic HTTPS question")
                    assert (await transport.post(root + "/ask", headers=auth(credential), json=body)).status_code == 200
                    assert (await transport.get("/api/conversations", headers=auth(credential))).status_code == 200
                    for method, path in (("PUT", "/api/conversations"), ("DELETE", root),
                        ("POST", root + "/messages"), ("GET", root + "/ask"),
                        ("GET", "/api/conversations/not-a-uuid/messages"),
                        ("GET", "/api/conversations/"), ("POST", root + "/ask/")):
                        assert (await transport.request(method, path, headers=auth(credential))).status_code == 404
                    for path in ("/api/%63onversations", "/api//conversations"):
                        assert (await transport.get(path, headers=auth(credential))).status_code in (403, 404)
                    assert h.llm.attempts == 1
                    with pytest.raises(InvalidStatus):
                        async with websocket(certificates, port, "/ws/assistant"): pytest.fail("Anonymous WSS accepted")
                    async with websocket(certificates, port, "/ws/assistant", credential) as ws:
                        await ws.send(json.dumps({**start(), "conversation_id": identity, "request_id": str(uuid4())}))
                        await ws.recv()
                        await ws.send(b"\0\0" * 100)
                        await ws.send(json.dumps(stop()))
                        events = [json.loads(await ws.recv()) for _ in range(3)]
                        assert events[-1]["type"] == "conversation.completed"
                        assert events[-1]["payload"]["turn"]["turn_number"] == 2
                    messages = (await transport.get(root + "/messages", headers=auth(credential))).json()["messages"]
                    assert [item["sequence"] for item in messages] == [1, 2, 3, 4]
                    assert [item["input_kind"] for item in messages] == ["text", None, "voice", None]
                    replay = await transport.post(root + "/ask", headers=auth(credential), json=body)
                    assert not replay.json()["created"] and h.llm.attempts == 2
    asyncio.run(asyncio.wait_for(run(), 120))
