"""Actual Qwen/BGE/CUDA Whisper through SECURE HTTP/WS on synthetic owned PostgreSQL."""
import asyncio
import io
import json
import logging
import os
import wave
from uuid import uuid4

import httpx
import pytest
from websockets.asyncio.client import connect

from backend.tests.integration.test_desktop_voice_question_whisper import synthetic_speech
from backend.tests.integration.test_security_uvicorn import server
from backend.tests.integration.test_sessions_postgres import paired, age_refresh
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.test_memory_ws import _start_message, _stop_message
from backend.tests.test_server_transport import start, stop
from kulai_memory import rag_runtime
from kulai_memory.app_factory import create_app
from kulai_memory.database_safety import database_snapshot_url, require_owned_database
from kulai_memory.embedding_provider import create_embedding_provider
from kulai_memory.security import Action, SecurityConfig, SecurityMode
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime
from kulai_memory.server.runtime import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings
from kulai_memory.whisper_provider import create_whisper_transcription_provider


def test_real_models_secure_note_chat_voice_reconnect_replay_and_speech(tmp_path, monkeypatch):
    if os.environ.get("KULAI_RUN_V2_REAL_FLOW") != "1":
        pytest.skip("Explicitly enable the synthetic V2 real-model flow; no downloads or production database.")
    assert os.name == "nt" and os.environ.get("KULAI_RUN_POSTGRES_INTEGRATION") == "1"
    assert os.environ.get("HF_HUB_OFFLINE") == "1", "Whisper must use existing cached model weights."
    counts = dict(qwen=0, bge=0, whisper=0)
    paths, traces = [], []
    original_llm = rag_runtime.create_llm_provider
    def llm(**kwargs):
        provider = original_llm(**kwargs)
        original = provider.generate
        async def generate(request):
            counts["qwen"] += 1
            return await original(request)
        provider.generate = generate
        return provider
    monkeypatch.setattr(rag_runtime, "create_llm_provider", llm)
    def bge(**kwargs):
        provider = create_embedding_provider(**kwargs)
        original = provider.embed
        async def embed(request):
            counts["bge"] += 1
            return await original(request)
        provider.embed = embed
        return provider
    def whisper(settings):
        provider = create_whisper_transcription_provider(settings=settings)
        original = provider.transcribe
        async def transcribe(request):
            counts["whisper"] += 1; paths.append(request.audio.path)
            result = await original(request)
            traces.append({"transcript": result.text})
            return result
        provider.transcribe = transcribe
        return provider
    def pcm(text, filename):
        with wave.open(io.BytesIO(synthetic_speech(tmp_path, text, filename)), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
            return audio.readframes(audio.getnframes())
    question_audio = pcm("How much profit do we make on ten notebooks before advertising costs?", "question.wav")
    note_audio = pcm("Synthetic price list. The Aurora package costs seventy nine zloty.", "note.wav")

    async def run():
        async with owned_store() as (owned, config, target, factory, _):
            await require_owned_database(owned, config=config)
            _, _, _, _, credential = await paired(factory, actions=frozenset(Action))
            headers = {"Authorization": "Bearer " + credential.access_token, "X-Kulai-Device-ID": str(credential.device_id)}
            settings = Settings(_env_file=None, kulai_vector_dimension=1024, kulai_cuda_dll_dir=Settings().kulai_cuda_dll_dir)
            assert (settings.kulai_llm_model, settings.kulai_embedding_model, settings.kulai_whisper_model) == (
                "qwen3.5:9b", "bge-m3:567m-fp16", "large-v3")
            assert (settings.kulai_whisper_device, settings.kulai_whisper_compute_type, settings.kulai_whisper_vad_filter) == (
                "cuda", "int8_float16", True)
            runtime = VoiceMemoryServerRuntime(settings=settings, provider_factory=whisper, embedding_provider_factory=bge,
                database_config_factory=lambda: target, engine_factory=lambda _: factory.kw["bind"],
                session_factory_builder=lambda _: factory)
            app = create_app(voice_runtime_factory=lambda: runtime, security_config=SecurityConfig(mode=SecurityMode.SECURE),
                security_runtime_factory=lambda: PostgresSecurityRuntime(config_factory=lambda: target))
            for logger in ("httpx", "kulai_memory.api.authentication", "faster_whisper"):
                logging.getLogger(logger).setLevel(logging.WARNING)
            async with server(app) as port:
                origin = f"http://127.0.0.1:{port}"
                async def voice(path, command, audio=b""):
                    async with connect(f"ws://127.0.0.1:{port}{path}", additional_headers=headers, proxy=None,
                                       close_timeout=5) as socket:
                        await socket.send(json.dumps(command)); events = [json.loads(await socket.recv())]
                        assert events[0]["type"] == "session.ready"
                        for index in range(0, len(audio), 32000): await socket.send(audio[index:index + 32000])
                        await socket.send(json.dumps(_stop_message() if path == "/ws/memory" else stop()))
                        events.extend([json.loads(await asyncio.wait_for(socket.recv(), 210)) for _ in range(3)])
                        await socket.wait_closed(); assert socket.close_code == 1000
                        return events
                async with httpx.AsyncClient(base_url=origin, headers=headers, timeout=210) as client:
                    async def create():
                        response = await client.post("/api/conversations", json={})
                        assert response.status_code == 201
                        return response.json()["conversation_id"]
                    async def ask(identity, question):
                        body = {"request_id": str(uuid4()), "question": question, "input_kind": "text"}
                        response = await client.post(f"/api/conversations/{identity}/ask", json=body)
                        assert response.status_code == 200, "Real conversation generation failed."
                        turn = response.json()["turn"]
                        traces.append({"question": question, "answer": turn["assistant"]["content"]})
                        return turn
                    first = await create()
                    first_turn = await ask(first, "Our synthetic project is Blue Lantern. We make A5 notebooks for 12 zloty, sell them for 30 zloty and have a 300 zloty promotion budget. Briefly repeat these amounts.")
                    assert not first_turn["assistant"]["memory_ids"]
                    request_id = str(uuid4())
                    command = {**start(), "conversation_id": first, "request_id": request_id}
                    spoken = await voice("/ws/assistant", command, question_audio)
                    assert spoken[-1]["type"] == "conversation.completed"
                    turn = spoken[-1]["payload"]["turn"]
                    assert turn["user"]["input_kind"] == "voice" and "180" in turn["assistant"]["content"]
                    traces.append({"voice_answer": turn["assistant"]["content"]})
                    before_replay = counts.copy()
                    replay = await voice("/ws/assistant", command)
                    assert replay[-1]["payload"] == {"turn": turn, "created": False} and counts == before_replay
                    await factory.kw["bind"].dispose()  # Force fresh physical PostgreSQL connections.
                    async with httpx.AsyncClient(base_url=origin, headers=headers, timeout=210) as reopened:
                        continued = await reopened.post(f"/api/conversations/{first}/ask", json={"request_id": str(uuid4()),
                            "question": "What were our original sale price, unit cost and promotion budget? Give only these three amounts with labels.", "input_kind": "text"})
                        assert continued.status_code == 200
                        answer = continued.json()["turn"]["assistant"]["content"]
                        assert all(value in answer for value in ("30", "12", "300"))
                        traces.append({"after_reconnect": answer})
                    before = await database_snapshot_url(target.async_url)
                    assert before.memories.count == before.vectors.count == 0
                    second = await create()
                    separate = await ask(second, "Our independent project is Green Bridge. Reply only with its name.")
                    assert "green bridge" in separate["assistant"]["content"].lower()
                    assert "lantern" not in separate["assistant"]["content"].lower()
                    saved = await voice("/ws/memory", _start_message(uuid4()), note_audio)
                    assert saved[-1]["type"] == "memory.saved"
                    memory_id = saved[-1]["payload"]["memory_id"]
                    detail = await client.get(f"/api/memories/{memory_id}")
                    assert detail.status_code == 200 and "aurora" in detail.json()["content"].lower()
                    assert len((await client.get("/api/memories")).json()["items"]) == 1
                    classic = await client.post("/api/assistant/ask", json={"query": "How much does Aurora cost according to the saved Memory?"})
                    assert classic.status_code == 200 and classic.json()["status"] == "answered"
                    assert [item["memory_id"] for item in classic.json()["citations"]] == [memory_id]
                    grounded = await ask(second, "What does the saved Memory say about the Aurora package price? Give its price.")
                    assert grounded["assistant"]["memory_ids"] == [memory_id] and "79" in grounded["assistant"]["content"]
                    before_replay = counts.copy()
                    result = await client.post(f"/api/conversations/{first}/ask", json={"request_id": first_turn["request_id"],
                        "question": first_turn["user"]["content"], "input_kind": "text"})
                    assert result.json() == {"turn": first_turn, "created": False} and counts == before_replay
                    answer = grounded["assistant"]["content"]
                    speech = await client.post("/api/assistant/speech-plan", json={"answer": answer})
                    assert speech.status_code == 200 and "".join(item["text"] for item in speech.json()["segments"]) == answer
                    await age_refresh(factory)
                    # Refresh accepts its capability alone, without inherited bearer/device headers.
                    async with httpx.AsyncClient(base_url=origin, timeout=10) as renewal:
                        refreshed = await renewal.post("/api/security/refresh", headers={"Authorization": "Refresh " + credential.refresh_credential},
                            json={key: str(getattr(credential, key)) for key in ("session_id", "device_id", "refresh_id")})
                    assert refreshed.status_code == 200
                    assert (await client.get("/api/conversations")).status_code == 401
                    history = await client.get(f"/api/conversations/{first}/messages", headers={"Authorization": "Bearer " + refreshed.json()["access_token"]})
                    assert [message["sequence"] for message in history.json()["messages"]] == list(range(1, 7))
                    final = await database_snapshot_url(target.async_url)
                    assert final.memories.count == final.vectors.count == 1
                    assert final.conversations.turns.count == 5 and final.conversations.messages.count == 10
                    assert counts["whisper"] == 2 and counts["bge"] >= 2 and all(not path.exists() for path in paths)
                    print("REAL_V2_FLOW=" + json.dumps({"models": ["qwen3.5:9b", "bge-m3:567m-fp16", "Whisper large-v3 CUDA int8_float16"],
                        "calls": counts, "trace": traces, "reconnect": True, "text_voice_replay": True,
                        "note_memory_classic_ask_speech_refresh": True, "conversations": 2, "turns": 5}, ensure_ascii=False), flush=True)
    asyncio.run(asyncio.wait_for(run(), 900))
