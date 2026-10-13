"""Real canonical Qwen, one server runtime, synthetic answers and owned DB only."""
import asyncio
import json
import os

import httpx
import pytest
from kulai_provider_ollama import provider as llm_module

from backend.tests.desktop_rag_fakes import FakeOwnedBGE
from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from backend.tests.integration.test_memory_retrieval_postgres import _seed_memories
from backend.tests.test_desktop_controller import FakeProvider
from backend.tests.test_server_speech import CASES
from backend.tests.local_transport import create_local_app as create_app
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.application.rag import MEMORY_RAG_SYSTEM_PROMPT
from kulai_memory.application import Memory
from kulai_memory.application.speech import SPEECH_PLANNER_SYSTEM_PROMPT, SpeechPlan, SpeechSegment, SpeechLanguage
from kulai_memory.database_safety import database_config_for_database
from kulai_memory.server.runtime import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings


def test_real_server_qwen_speech_endpoint_exact_languages_one_client_zero_checkout(monkeypatch):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_LLM_INTEGRATION") != "1":
        pytest.skip("Enable synthetic Qwen speech planner integration.")
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw["bind"]
            counts = dict(clients=0, requests=0, speech_requests=0, rag_requests=0, closed=0)
            answers = ["".join(t for t, _ in parts) for parts in CASES]
            class Client(llm_module.AsyncClient):
                def __init__(self, *args, **kwargs):
                    counts["clients"] += 1
                    super().__init__(*args, **kwargs)
                async def chat(self, *args, **kwargs):
                    assert engine.pool.checkedout() == 0
                    assert kwargs["model"] == "qwen3.5:9b"
                    messages = kwargs["messages"]
                    if messages[0]["content"] == SPEECH_PLANNER_SYSTEM_PROMPT:
                        assert len(messages) == 2
                        assert json.loads(messages[1]["content"].split("\n", 1)[1]) in [{"answer": a} for a in answers]
                        counts["speech_requests"] += 1
                    else:
                        assert len(messages) == 3 and messages[0]["content"] == MEMORY_RAG_SYSTEM_PROMPT
                        counts["rag_requests"] += 1
                    counts["requests"] += 1
                    return await super().chat(*args, **kwargs)
                async def close(self):
                    counts["closed"] += 1
                    await super().close()
            monkeypatch.setattr(llm_module, "AsyncClient", Client)
            bge, whisper = FakeOwnedBGE(), FakeProvider(("synthetic",))
            runtime = VoiceMemoryServerRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024,
                                                                 kulai_llm_model="qwen3.5:9b"),
                provider_factory=lambda ignored: whisper, embedding_provider_factory=lambda **kw: bge,
                database_config_factory=lambda: database_config_for_database(engine.url.database),
                engine_factory=lambda ignored: engine, session_factory_builder=lambda ignored: factory)
            app = create_app(voice_runtime_factory=lambda: runtime)
            async with app.router.lifespan_context(app):
                before = await snapshot(factory)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                    for answer, parts in zip(answers, CASES):
                        response = await client.post("/api/assistant/speech-plan", json={"answer": answer})
                        assert response.status_code == 200
                        segments = response.json()["segments"]
                        assert "".join(s["text"] for s in segments) == answer
                        assert [s["language"] for s in segments] == [lang for _, lang in parts]
                        labels = [s["language"] for s in segments for _ in s["text"]]
                        for phrase, language in parts:
                            phrase = phrase.strip()
                            offset = answer.index(phrase)
                            assert labels[offset:offset + len(phrase)] == [language] * len(phrase)
                        print("server.speech.real.languages=" + "/".join(s["language"] for s in segments))
                    response = await client.post("/api/assistant/speech-plan", json={"answer": INSUFFICIENT_CONTEXT_ANSWER})
                    assert response.json() == {"segments": [{"text": INSUFFICIENT_CONTEXT_ANSWER, "language": "pl"}]}
                assert await snapshot(factory) == before and engine.pool.checkedout() == 0
                assert not bge.requests and not whisper.requests
                # Same runtime and Qwen also handle a canonical RAG answer and its speech plan.
                memory = Memory(content="The green dragon currently lives on Venus.")
                await _seed_memories(factory, (memory,))
                await runtime._indexer.ensure(memory=memory)
                fixture_snapshot = await snapshot(factory)
                result = await runtime.ask_memory(query="Gdzie mieszka zielony smok?")
                assert result.sufficient_context and [c.memory_id for c in result.citations] == [memory.id]
                answers.append(result.answer)
                embeddings_before = len(bge.requests)
                plan = await runtime.plan_speech(answer=result.answer)
                assert plan.original_answer == result.answer and len(bge.requests) == embeddings_before
                assert await snapshot(factory) == fixture_snapshot
            assert counts == dict(clients=1, requests=len(CASES) + 2, speech_requests=len(CASES) + 1,
                                 rag_requests=1, closed=1) and bge.closed == 1
            print("server.speech.real.qwen=" + json.dumps(counts) + ";db_checkout=0;owned_snapshot_identical=true;bge_during_plans=0;whisper=0")
    asyncio.run(asyncio.wait_for(scenario(), 600))
