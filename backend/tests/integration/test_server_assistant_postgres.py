"""One server over owned synthetic PostgreSQL only; no main content enters models."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from kulai_provider_ollama import provider as llm_module
from kulai_provider_ollama_embeddings import provider as bge_module

from backend.tests.assistant_transport_fakes import AsgiSocket
from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_rag_postgres import DRAGON, SOUP, SyntheticBGE, CheckedLLM
from backend.tests.integration.test_memory_lifecycle_postgres import mutate_only, snapshot
from backend.tests.integration.test_memory_retrieval_postgres import _observe_reads, _seed_memories
from backend.tests.test_desktop_controller import FakeProvider
from backend.tests.test_memory_ws import _start_message, _stop_message
from kulai_memory import rag_runtime
from backend.tests.local_transport import create_local_app as create_app
from kulai_memory.application import Memory
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.database_safety import database_config_for_database
from kulai_memory.server.runtime import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings

QUESTION = "Gdzie mieszka zielony smok?"
UNSUPPORTED = "Jaki jest ulubiony instrument smoka?"


async def note(app, pcm=b"\0\0" * 100, *, timeout=5):
    ws = await AsgiSocket(app, "/ws/memory", timeout=timeout).connect()
    await ws.send_json(_start_message(uuid4()))
    events = [await ws.event()]
    from kulai_memory.api.pcm_wav import MAX_BINARY_FRAME_BYTES
    for offset in range(0, len(pcm), MAX_BINARY_FRAME_BYTES):
        await ws.send_bytes(pcm[offset:offset + MAX_BINARY_FRAME_BYTES])
    await ws.send_json(_stop_message())
    events.extend([await ws.event() for _ in range(3)])
    await ws.closed()
    assert [e["type"] for e in events] == ["session.ready", "transcript.final", "memory.saving", "memory.saved"]
    return UUID(events[-1]["payload"]["memory_id"])


async def readonly(factory, operation):
    before = await snapshot(factory)
    statements, commits, remove = _observe_reads(factory.kw["bind"])
    try:
        result = await operation()
    finally:
        remove()
    assert not commits and not set(statements) - {"SELECT", "SET"}
    assert await snapshot(factory) == before
    return result


@pytest.mark.parametrize("real", [False, True], ids=["controlled", "real_bge_qwen"])
def test_owned_server_history_text_voice_rag_lifecycle_and_provider_reuse(monkeypatch, real):
    _require_opt_in()
    if real and not all(os.environ.get(k) == "1" for k in ("KULAI_RUN_OLLAMA_INTEGRATION", "KULAI_RUN_LLM_INTEGRATION")):
        pytest.skip("Enable real BGE and Qwen integrations for owned synthetic server RAG.")

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw["bind"]
            counts = dict(whisper=0, engine=0, bge_clients=0, bge_requests=0, bge_closed=0,
                          qwen_clients=0, qwen_requests=0, qwen_closed=0)
            checkouts, paths = [], []
            stt = FakeProvider((DRAGON,))
            class OwnedBGE(SyntheticBGE):
                async def aclose(self):
                    self.closed += 1
            bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
            kwargs = {}
            if real:
                class BGEClient(bge_module.AsyncClient):
                    def __init__(self, *args, **kw):
                        counts["bge_clients"] += 1
                        super().__init__(*args, **kw)
                    async def embed(self, *args, **kw):
                        assert engine.pool.checkedout() == 0 and kw.get("dimensions") is None
                        counts["bge_requests"] += 1
                        response = await super().embed(*args, **kw)
                        assert len(response.embeddings[0]) == 1024
                        return response
                    async def close(self):
                        counts["bge_closed"] += 1
                        await super().close()
                class QwenClient(llm_module.AsyncClient):
                    def __init__(self, *args, **kw):
                        counts["qwen_clients"] += 1
                        super().__init__(*args, **kw)
                    async def chat(self, *args, **kw):
                        checkouts.append(engine.pool.checkedout())
                        assert checkouts[-1] == 0 and kw["model"] == "qwen3.5:9b"
                        counts["qwen_requests"] += 1
                        return await super().chat(*args, **kw)
                    async def close(self):
                        counts["qwen_closed"] += 1
                        await super().close()
                monkeypatch.setattr(bge_module, "AsyncClient", BGEClient)
                monkeypatch.setattr(llm_module, "AsyncClient", QwenClient)
            else:
                counts["bge_clients"] = counts["qwen_clients"] = 1
                kwargs["embedding_provider_factory"] = lambda **kw: bge
                monkeypatch.setattr(rag_runtime, "create_llm_provider", lambda **kw: llm)
            original_stt = stt.transcribe
            async def tracked_stt(request):
                assert engine.pool.checkedout() == 0
                paths.append(request.audio.path)
                return await original_stt(request)
            stt.transcribe = tracked_stt
            def whisper_provider(settings):
                counts["whisper"] += 1
                return stt
            def runtime_engine(config):
                counts["engine"] += 1
                return engine
            runtime = VoiceMemoryServerRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
                provider_factory=whisper_provider,
                database_config_factory=lambda: database_config_for_database(engine.url.database),
                engine_factory=runtime_engine, session_factory_builder=lambda ignored: factory, **kwargs)
            app = create_app(voice_runtime_factory=lambda: runtime)
            async with app.router.lifespan_context(app):
                assert counts["bge_requests"] == counts["qwen_requests"] == 0
                dragon_id = await note(app)
                stt._texts = (SOUP,)
                await note(app)
                assert runtime._indexer.provider is runtime._embedding_provider is runtime._rag._retrieval._provider
                llm.output["used_memory_ids"] = [str(dragon_id)]
                before = await snapshot(factory)
                assert before.memories.count == before.vectors.count == 2 and before.tombstones.count == 0
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
                    history = await readonly(factory, lambda: client.get("/api/memories", params={"limit": 1}))
                    page = history.json()
                    second = await client.get("/api/memories", params={"limit": 1, **page["next_cursor"]})
                    assert len(page["items"]) == len(second.json()["items"]) == 1
                    assert page["items"][0]["id"] != second.json()["items"][0]["id"]
                    assert second.json()["next_cursor"] is None
                    detail = await readonly(factory, lambda: client.get(f"/api/memories/{dragon_id}"))
                    assert detail.json()["content"] == DRAGON
                    answer = await readonly(factory, lambda: client.post("/api/assistant/ask", json={"query": QUESTION}))
                    assert answer.status_code == 200 and answer.json()["status"] == "answered"
                    assert "venus" in answer.json()["answer"].lower() or "wenus" in answer.json()["answer"].lower()
                    assert [x["memory_id"] for x in answer.json()["citations"]] == [str(dragon_id)]
                    if not real:
                        llm.output = dict(answer="discarded", used_memory_ids=[], sufficient_context=False)
                    insufficient = await readonly(factory, lambda: client.post("/api/assistant/ask", json={"query": UNSUPPORTED}))
                    assert insufficient.json() == dict(status="insufficient_context", answer=INSUFFICIENT_CONTEXT_ANSWER, citations=[])
                    for supported in (True, False):
                        stt._texts = (QUESTION if supported else UNSUPPORTED,)
                        if not real:
                            llm.output = dict(answer="Wenus", used_memory_ids=[str(dragon_id)] if supported else [],
                                              sufficient_context=supported)
                        events = await readonly(factory, lambda: AsgiSocket(app).question())
                        assert [e["type"] for e in events] == ["session.ready", "transcript.final", "rag.started", "assistant.completed"]
                        assert [e["sequence"] for e in events] == [1, 2, 3, 4]
                        payload = events[-1]["payload"]
                        assert payload["status"] == ("answered" if supported else "insufficient_context")
                        assert [x["memory_id"] for x in payload["citations"]] == ([str(dragon_id)] if supported else [])
                    assert await snapshot(factory) == before
                    print("server.owned.snapshot_before=" + json.dumps(asdict(before), sort_keys=True))
                    print("server.owned.snapshot_after=" + json.dumps(asdict(await snapshot(factory)), sort_keys=True))
                    async with factory() as session:
                        from kulai_memory.persistence import PostgresMemoryRepository
                        dragon = await PostgresMemoryRepository(db=session).get_by_id(dragon_id)
                    await mutate_only(factory, "archive", dragon)
                    active = (await client.get("/api/memories")).json()["items"]
                    archived = (await client.get("/api/memories", params={"archived": "true"})).json()["items"]
                    assert str(dragon_id) not in [m["id"] for m in active]
                    assert [m["id"] for m in archived] == [str(dragon_id)]
                    if not real:
                        llm.output = dict(answer="discarded", used_memory_ids=[], sufficient_context=False)
                        llm.excluded_id = str(dragon_id)
                    excluded = await readonly(factory, lambda: client.post("/api/assistant/ask", json={"query": QUESTION}))
                    assert excluded.json()["status"] == "insufficient_context" and not excluded.json()["citations"]
                    await mutate_only(factory, "restore", dragon)
                    await runtime.reconcile_missing_indexes()
                    if not real:
                        llm.excluded_id = None
                        llm.output = dict(answer="Wenus", used_memory_ids=[str(dragon_id)], sufficient_context=True)
                    restored = await readonly(factory, lambda: client.post("/api/assistant/ask", json={"query": QUESTION}))
                    assert restored.json()["status"] == "answered"
                    assert [x["memory_id"] for x in restored.json()["citations"]] == [str(dragon_id)]
            assert len(stt.requests) == 4 and all(not p.exists() for p in paths)
            if not real:
                counts.update(bge_requests=bge.requests, bge_closed=bge.closed,
                              qwen_requests=len(llm.calls), qwen_closed=llm.closed)
                checkouts = llm.checkout_observations
            assert counts["bge_clients"] == counts["bge_closed"] == counts["qwen_clients"] == counts["qwen_closed"] == 1
            assert counts["whisper"] == counts["engine"] == 1
            assert counts["bge_requests"] == 9 and counts["qwen_requests"] == 6 and set(checkouts) == {0}
            print("server.providers." + ("real=" if real else "controlled=") + json.dumps(counts, sort_keys=True))
            print("server.owned.history_text_voice=PASS;question_writes=0;checkout_during_qwen=0;native_dimension=1024;dimensions_override_count=0")
    asyncio.run(asyncio.wait_for(scenario(), 600 if real else 120))


def test_owned_postgres_equal_timestamp_cursor_no_duplicate_or_skip():
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            from kulai_memory.history_persistence import read_memory_history
            stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
            memories = tuple(Memory(id=UUID(int=i), created_at=stamp, content="synthetic history " + str(i))
                             for i in range(1, 8))
            await _seed_memories(factory, memories)
            before = await snapshot(factory)
            ids, cursor = [], None
            while True:
                page = await read_memory_history(archived=False, limit=2, cursor=cursor, session_factory=factory)
                ids.extend(m.id for m in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert ids == [UUID(int=i) for i in range(7, 0, -1)]
            assert await snapshot(factory) == before
    asyncio.run(scenario())


def test_owned_server_failed_cancelled_empty_questions_leave_complete_snapshot_unchanged(monkeypatch):
    _require_opt_in()
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw["bind"]
            class OwnedBGE(SyntheticBGE):
                async def aclose(self):
                    self.closed += 1
            bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
            monkeypatch.setattr(rag_runtime, "create_llm_provider", lambda **kw: llm)
            stt = FakeProvider((DRAGON,))
            runtime = VoiceMemoryServerRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
                provider_factory=lambda ignored: stt, embedding_provider_factory=lambda **kw: bge,
                database_config_factory=lambda: database_config_for_database(engine.url.database),
                engine_factory=lambda ignored: engine, session_factory_builder=lambda ignored: factory)
            app = create_app(voice_runtime_factory=lambda: runtime)
            async with app.router.lifespan_context(app):
                dragon_id = await note(app)
                llm.output["used_memory_ids"] = [str(dragon_id)]
                before = await snapshot(factory)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
                    llm.error = RuntimeError("PRIVATE_MODEL_SENTINEL")
                    failed = await readonly(factory, lambda: client.post("/api/assistant/ask", json={"query": QUESTION}))
                    assert failed.status_code == 500 and "PRIVATE" not in failed.text
                    llm.error = None
                    stt._texts = ("",)
                    requests = bge.requests
                    empty = await readonly(factory, lambda: AsgiSocket(app).question())
                    assert [e["type"] for e in empty] == ["session.ready", "transcript.final"]
                    assert bge.requests == requests
                    stt._texts = (QUESTION,)
                    original = stt.transcribe
                    async def fail(request):
                        raise RuntimeError("PRIVATE_STT_SENTINEL")
                    stt.transcribe = fail
                    ws = await AsgiSocket(app).connect()
                    from backend.tests.test_server_transport import start, stop
                    await ws.send_json(start())
                    await ws.event()
                    await ws.send_bytes(b"\0\0" * 100)
                    await ws.send_json(stop())
                    error = await ws.event()
                    assert error["type"] == "error" and "PRIVATE" not in str(error)
                    await ws.closed(1011)
                    assert await snapshot(factory) == before
                    stt.transcribe = original
                    entered, cancelled = asyncio.Event(), asyncio.Event()
                    async def block(request):
                        assert engine.pool.checkedout() == 0
                        entered.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cancelled.set()
                    llm.generate = block
                    ws = await AsgiSocket(app).connect()
                    await ws.send_json(start())
                    await ws.event()
                    await ws.send_bytes(b"\0\0" * 100)
                    await ws.send_json(stop())
                    await ws.event()
                    await ws.event()
                    await entered.wait()
                    await ws.disconnect()
                    await asyncio.wait_for(ws.task, 2)
                    assert cancelled.is_set() and ws.outgoing.empty()
                    assert await snapshot(factory) == before and engine.pool.checkedout() == 0
                    assert not runtime._inference_tasks and not runtime._ai_callers
            assert bge.closed == llm.closed == 1
            print("server.owned.failure_empty_cancellation_snapshot=UNCHANGED;question_writes=0;checkout=0")
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_real_whisper_shared_by_note_and_assistant_question(tmp_path, monkeypatch):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_WHISPER_INTEGRATION") != "1" or os.name != "nt":
        pytest.skip("Enable real local Windows/CUDA Whisper for controlled synthetic server audio.")
    import io
    import wave
    from backend.tests.integration.test_desktop_voice_question_whisper import synthetic_speech
    from kulai_memory.whisper_provider import create_whisper_transcription_provider
    from kulai_memory.api import assistant_ws, memory_ws
    from kulai_memory.api.pcm_wav import OwnedPcmWav
    def pcm(text, filename):
        with wave.open(io.BytesIO(synthetic_speech(tmp_path, text, filename)), "rb") as stream:
            assert stream.getframerate() == 16000 and stream.getnchannels() == 1 and stream.getsampwidth() == 2
            return stream.readframes(stream.getnframes())
    note_audio = pcm(DRAGON, "synthetic-note.wav")
    question_audio = pcm("Where does the green dragon live?", "synthetic-question.wav")
    paths = []
    def owned_wav():
        wav = OwnedPcmWav()
        paths.append(wav.path)
        return wav
    monkeypatch.setattr(assistant_ws, "OwnedPcmWav", owned_wav)
    monkeypatch.setattr(memory_ws, "OwnedPcmWav", owned_wav)

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw["bind"]
            class OwnedBGE(SyntheticBGE):
                async def aclose(self):
                    self.closed += 1
            bge, llm = OwnedBGE(engine), CheckedLLM(engine, Memory(content=DRAGON))
            monkeypatch.setattr(rag_runtime, "create_llm_provider", lambda **kw: llm)
            counts = dict(providers=0, models=0, inference_requests=0)
            providers = []
            def whisper(settings):
                counts["providers"] += 1
                p = create_whisper_transcription_provider(settings=settings)
                load, transcribe = p._model_factory, p.transcribe
                def model(*args, **kwargs):
                    counts["models"] += 1
                    return load(*args, **kwargs)
                async def run(request):
                    assert engine.pool.checkedout() == 0
                    counts["inference_requests"] += 1
                    return await transcribe(request)
                p._model_factory, p.transcribe = model, run
                providers.append(p)
                return p
            runtime = VoiceMemoryServerRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024,
                kulai_cuda_dll_dir=Settings().kulai_cuda_dll_dir), provider_factory=whisper,
                embedding_provider_factory=lambda **kw: bge,
                database_config_factory=lambda: database_config_for_database(engine.url.database),
                engine_factory=lambda ignored: engine, session_factory_builder=lambda ignored: factory)
            app = create_app(voice_runtime_factory=lambda: runtime)
            async with app.router.lifespan_context(app):
                dragon_id = await note(app, note_audio, timeout=180)
                async with factory() as session:
                    from kulai_memory.persistence import PostgresMemoryRepository
                    dragon = await PostgresMemoryRepository(db=session).get_by_id(dragon_id)
                assert "dragon" in dragon.content.lower()
                llm.expected_content = dragon.content
                llm.output["used_memory_ids"] = [str(dragon_id)]
                loaded = providers[0]._model
                before = await snapshot(factory)
                events = await readonly(factory, lambda: AsgiSocket(app).question(pcm=question_audio))
                assert "dragon" in events[1]["payload"]["text"].lower()
                assert events[-1]["payload"]["status"] == "answered"
                assert [c["memory_id"] for c in events[-1]["payload"]["citations"]] == [str(dragon_id)]
                assert providers[0]._model is loaded and runtime.provider is providers[0]
                assert await snapshot(factory) == before and all(not p.exists() for p in paths)
            assert counts == dict(providers=1, models=1, inference_requests=2)
            assert bge.requests == 2 and bge.closed == llm.closed == 1
            print("server.whisper.real=" + json.dumps(counts, sort_keys=True) + ";model=large-v3;cuda;int8_float16;VAD=true;audio_retained=0;question_writes=0")
    asyncio.run(asyncio.wait_for(scenario(), 300))
