"""Synthetic local transport tests, including real ASGI disconnect handling."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.tests.desktop_rag_fakes import FakeDesktopRag
from backend.tests.test_desktop_controller import InMemoryRepository, FakeSessionFactory
from backend.tests.test_memory_ws import FakeRuntime
from kulai_memory.api import pcm_wav
from kulai_memory.api.assistant_contract import AssistantResponse
from kulai_memory.api.cancellation import ClientDisconnected, until_disconnect
from kulai_memory.app_factory import create_app
from kulai_memory.application import Memory
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER, MemoryCitation
from kulai_memory.history_persistence import read_memory_detail, read_memory_history

PRIVATE = "private sentinel query/context/password"


class HistoryRepository(InMemoryRepository):
    async def list_history(self, *, archived, limit, cursor=None):
        items = await self.list_library(archived=archived, limit=100)
        if cursor:
            items = tuple(m for m in items if (m.created_at, m.id) < (cursor.before_created_at, cursor.before_id))
        return items[:limit + 1]


class TransportRuntime(FakeRuntime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rag = FakeDesktopRag()
        self.repository, self.sessions = HistoryRepository(), FakeSessionFactory()
        self.paths = []

    async def list_memories(self, **kwargs):
        return await read_memory_history(**kwargs, session_factory=self.sessions,
                                         repository_factory=lambda ignored: self.repository)

    async def get_memory(self, **kwargs):
        return await read_memory_detail(**kwargs, session_factory=self.sessions,
                                        repository_factory=lambda ignored: self.repository)

    async def ask_memory(self, **kwargs):
        return await self.rag.ask(**kwargs)

    async def transcribe_question(self, *, audio_path):
        from kulai_transcription import AudioPathInput, TranscriptionRequest
        self.paths.append(audio_path)
        assert audio_path.is_file()
        return await self.service.transcribe(request=TranscriptionRequest(audio=AudioPathInput(path=audio_path)))


def start(top_k=5):
    return dict(schema_version=1, type="question.start", top_k=top_k,
                audio=dict(encoding="pcm_s16le", sample_rate=16000, channels=1))


def stop():
    return dict(schema_version=1, type="question.stop")


def answered(runtime):
    runtime.rag.result = runtime.rag.result.model_copy(update={
        "sufficient_context": True, "answer": "Venus",
        "citations": (MemoryCitation(memory_id=UUID(int=8), rank=2, score=.7395),),
    })


def assert_closed(ws, code):
    with pytest.raises(WebSocketDisconnect) as caught:
        ws.receive_json()
    assert caught.value.code == code


def test_history_cursor_partition_preview_detail_and_privacy():
    runtime = TransportRuntime()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = [Memory(id=UUID(int=i), content="  hello\n\t" + "x" * 230,
                    created_at=stamp, metadata={"private": PRIVATE},
                    archived_at=stamp if i == 4 else None) for i in range(1, 5)]
    for m in items:
        runtime.repository.by_ingestion[m.ingestion_id] = m
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        first = client.get("/api/memories", params={"limit": 2}).json()
        assert [i["id"] for i in first["items"]] == [str(UUID(int=3)), str(UUID(int=2))]
        assert all(len(i["content_preview"]) == 200 for i in first["items"])
        assert first["items"][0]["content_preview"].startswith("hello x")
        second = client.get("/api/memories", params={"limit": 2, **first["next_cursor"]}).json()
        assert [i["id"] for i in second["items"]] == [str(UUID(int=1))]
        assert second["next_cursor"] is None
        archived = client.get("/api/memories", params={"archived": "true"}).json()
        assert [i["id"] for i in archived["items"]] == [str(UUID(int=4))]
        for m in items:
            detail = client.get(f"/api/memories/{m.id}").json()
            assert detail["content"] == m.content and detail["archived"] == (m.archived_at is not None)
            assert set(detail) == {"id", "created_at", "revision", "archived", "content"}
        response = client.get(f"/api/memories/{UUID(int=100)}")
        assert response.status_code == 404 and response.json() == {"detail": "Memory not found."}
        assert PRIVATE not in str(first) + str(archived)
    assert all(s.closed and s.commits == 0 for s in runtime.sessions.sessions)
    assert not runtime.rag.requests and not runtime.provider.call_count


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": "bad"},
    {"before_id": str(UUID(int=1))}, {"before_created_at": "2026-01-01T00:00:00Z"},
    {"before_created_at": "2026-01-01T00:00:00", "before_id": str(UUID(int=1))},
    {"before_created_at": PRIVATE, "before_id": PRIVATE}])
def test_history_invalid_request_is_safe(params):
    runtime = TransportRuntime()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        response = client.get("/api/memories", params=params)
        assert response.status_code == 422 and PRIVATE not in response.text
    assert runtime.sessions.sessions == []


def test_empty_history_and_bounded_safe_errors():
    runtime = TransportRuntime()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        assert client.get("/api/memories").json() == {"items": [], "next_cursor": None}
        runtime.rag.error = RuntimeError(PRIVATE)
        response = client.post("/api/assistant/ask", json={"query": PRIVATE})
        assert response.status_code == 500
        assert response.json() == {"detail": "Memory assistant could not complete the operation."}
    from kulai_memory.server.runtime import ServerDatabaseError
    runtime = TransportRuntime(startup_error=ServerDatabaseError())
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        assert client.get("/api/memories").status_code == 503
        assert client.post("/api/assistant/ask", json={"query": "q"}).json() == {
            "detail": "Memory assistant is unavailable."}
        with client.websocket_connect("/ws/assistant") as ws:
            assert ws.receive_json()["type"] == "error"
            assert_closed(ws, 1013)


@pytest.mark.parametrize("code,stage", [("rag.retrieval_failed", "rag_retrieval"),
    ("rag.generation_failed", "rag_generation"), ("rag.invalid_citations", "rag_citations")])
def test_safe_internal_error_stage_without_payload_or_query(caplog, code, stage):
    import logging
    from kulai_memory.application.rag import MemoryRagError
    runtime = TransportRuntime()
    runtime.rag.error = MemoryRagError(code)
    app = create_app(voice_runtime_factory=lambda: runtime)
    logger = logging.getLogger("kulai_memory.api.memory_http")
    logger.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            response = client.post("/api/assistant/ask", json={"query": PRIVATE})
            assert response.status_code == 500 and PRIVATE not in response.text
    finally:
        logger.removeHandler(caplog.handler)
    messages = [r.getMessage() for r in caplog.records if r.name == "kulai_memory.api.memory_http"]
    assert messages == [f"assistant request failed stage={stage} error_type=MemoryRagError code={code}"]
    assert PRIVATE not in " ".join(messages)


def test_invalid_utf8_json_is_safe_400_before_rag():
    runtime = TransportRuntime()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        response = client.post("/api/assistant/ask", content=b'{"query":"invalid-\xf3"}',
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid assistant request."}
    assert not runtime.rag.requests and not runtime.sessions.sessions


@pytest.mark.parametrize("body", [{"query": ""}, {"query": " \t"}, {"query": "x" * 10001},
    {"query": 1}, {"query": "q", "top_k": 0}, {"query": "q", "top_k": 21},
    {"query": "q", "top_k": True}, {"query": "q", "extra": PRIVATE}])
def test_invalid_text_ask_never_calls_rag(body):
    runtime = TransportRuntime()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        r = client.post("/api/assistant/ask", json=body)
        assert r.status_code == 422 and r.json() == {"detail": "Invalid assistant request."}
    assert not runtime.rag.requests


@pytest.mark.parametrize("sufficient", [False, True])
def test_http_answer_projection_exact_no_context_and_no_writes(sufficient):
    runtime = TransportRuntime()
    if sufficient:
        answered(runtime)
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        r = client.post("/api/assistant/ask", json={"query": "exact question", "top_k": 3})
        assert r.status_code == 200
        assert r.json() == AssistantResponse.from_result(runtime.rag.result).model_dump(mode="json")
        assert set(r.json()) == {"status", "answer", "citations"}
    assert runtime.rag.requests == [("exact question", 3)]
    assert runtime.ingest_calls == runtime.sessions.sessions == []


@pytest.mark.parametrize("sufficient", [False, True])
def test_voice_event_order_answer_citations_audio_cleanup_zero_ingestion(sufficient):
    runtime = TransportRuntime(text="Gdzie mieszka smok?")
    if sufficient:
        answered(runtime)
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/assistant") as ws:
            ws.send_json(start(3))
            events = [ws.receive_json()]
            ws.send_bytes(b"\0\0" * 100)
            ws.send_json(stop())
            events += [ws.receive_json() for _ in range(3)]
            assert_closed(ws, 1000)
    assert [e["type"] for e in events] == ["session.ready", "transcript.final", "rag.started", "assistant.completed"]
    assert [e["sequence"] for e in events] == [1, 2, 3, 4]
    assert len({e["session_id"] for e in events}) == 1
    assert events[-1]["payload"] == AssistantResponse.from_result(runtime.rag.result).model_dump(mode="json")
    assert runtime.rag.requests == [("Gdzie mieszka smok?", 3)]
    assert runtime.ingest_calls == runtime.sessions.sessions == []
    assert runtime.paths and all(not p.exists() for p in runtime.paths)


@pytest.mark.parametrize("command", [stop(), start(0), start(21), start(True),
    {**start(), "ingestion_id": str(UUID(int=1))}, {**start(), "schema_version": 2}])
def test_invalid_voice_command(command):
    runtime = TransportRuntime()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/assistant") as ws:
            ws.send_json(command)
            assert ws.receive_json()["type"] == "error"
            assert_closed(ws, 1002)
    assert not runtime.provider.call_count and not runtime.rag.requests


@pytest.mark.parametrize("case", ["before", "duplicate", "odd", "large", "total"])
def test_voice_order_and_audio_limits_cleanup(monkeypatch, case):
    runtime = TransportRuntime()
    paths = []
    original = pcm_wav.OwnedPcmWav
    def tracked():
        wav = original()
        paths.append(wav.path)
        return wav
    from kulai_memory.api import assistant_ws
    monkeypatch.setattr(assistant_ws, "OwnedPcmWav", tracked)
    if case == "total":
        monkeypatch.setattr(pcm_wav, "MAX_TOTAL_AUDIO_BYTES", 4)
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/assistant") as ws:
            if case != "before":
                ws.send_json(start())
                assert ws.receive_json()["type"] == "session.ready"
            if case == "duplicate":
                ws.send_json(start())
            else:
                ws.send_bytes(b"x" * (pcm_wav.MAX_BINARY_FRAME_BYTES + 2) if case == "large"
                              else b"123456" if case == "total" else b"x")
            assert ws.receive_json()["type"] == "error"
            assert_closed(ws, 1009 if case in ("large", "total") else 1002)
    assert all(not p.exists() for p in paths)
    assert not runtime.provider.call_count and not runtime.rag.requests


@pytest.mark.parametrize("text", ["", "  \n"])
def test_empty_speech_no_rag(text):
    runtime = TransportRuntime(text=text)
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/assistant") as ws:
            ws.send_json(start())
            ws.receive_json()
            ws.send_bytes(b"\0\0" * 100)
            ws.send_json(stop())
            assert ws.receive_json()["payload"]["text"] == text
            assert_closed(ws, 1000)
    assert not runtime.rag.requests and not runtime.ingest_calls
    assert all(not p.exists() for p in runtime.paths)


def test_empty_audio_closes_without_models_or_rag():
    runtime = TransportRuntime()
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/assistant") as ws:
            ws.send_json(start())
            ws.receive_json()
            ws.send_json(stop())
            assert ws.receive_json()["payload"] == {"text": ""}
            assert_closed(ws, 1000)
    assert runtime.provider.call_count == 0 and not runtime.rag.requests and not runtime.ingest_calls


@pytest.mark.parametrize("stage", ["stt", "rag"])
def test_voice_failure_is_safe_and_cleans_audio(stage):
    runtime = TransportRuntime()
    if stage == "stt":
        async def fail(**kwargs):
            raise RuntimeError(PRIVATE)
        runtime.transcribe_question = fail
    else:
        runtime.rag.error = RuntimeError(PRIVATE)
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        with client.websocket_connect("/ws/assistant") as ws:
            ws.send_json(start())
            ws.receive_json()
            ws.send_bytes(b"\0\0" * 100)
            ws.send_json(stop())
            if stage == "rag":
                ws.receive_json()
                ws.receive_json()
            e = ws.receive_json()
            assert e["type"] == "error" and PRIVATE not in str(e)
            assert_closed(ws, 1011)
    assert all(not p.exists() for p in runtime.paths) and not runtime.ingest_calls


def test_disconnect_and_cancellation_cancel_work_without_polling():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def work():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}
        with pytest.raises(ClientDisconnected):
            await until_disconnect(work(), receive)
        assert cancelled.is_set()
        async def wait_forever():
            await asyncio.Event().wait()
        entered.clear()
        task = asyncio.create_task(until_disconnect(work(), wait_forever))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
