"""Synthetic speech endpoint and production AI-lane tests; no main data."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.tests.speech_fakes import PlannerLLM
from backend.tests.test_server_assistant_runtime import Harness, Probe
from backend.tests.test_server_transport import TransportRuntime
from kulai_memory.app_factory import create_app
from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER
from kulai_memory.application.speech import SpeechError, plan_speech
from kulai_memory.server.runtime import ServerRuntimeError

CASES = [
    [("To jest odpowiedź.", "pl")], [("This is an answer.", "en")],
    [("To jest test. ", "pl"), ("This is a test.", "en")],
    [("This is a test. ", "en"), ("To jest test.", "pl")],
    [("To jest odpowiedź. ", "pl"), ("This is English. ", "en"), ("I znowu polski.", "pl")],
    [("This is English. ", "en"), ("To jest polski. ", "pl"), ("English again.", "en")],
]


class SpeechTransportRuntime(TransportRuntime):
    def __init__(self, parts):
        super().__init__()
        self.planner = PlannerLLM(parts)
        self.answers = []

    async def plan_speech(self, *, answer):
        self.answers.append(answer)
        return await plan_speech(answer=answer, provider=self.planner)


@pytest.mark.parametrize("parts", CASES)
def test_endpoint_exact_languages_projection_and_only_answer_data(parts):
    answer = "".join(t for t, _ in parts)
    runtime = SpeechTransportRuntime(parts)
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        response = client.post("/api/assistant/speech-plan", json={"answer": answer})
    assert response.status_code == 200
    result = response.json()
    assert result == {"segments": [{"text": t, "language": lang} for t, lang in parts]}
    assert "".join(s["text"] for s in result["segments"]) == answer
    assert runtime.answers == [answer]
    messages = runtime.planner.calls[0].messages
    assert len(messages) == 2
    assert json.loads(messages[1].content.split("\n", 1)[1]) == {"answer": answer}


def test_endpoint_insufficient_uses_no_qwen():
    runtime = SpeechTransportRuntime([])
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        response = client.post("/api/assistant/speech-plan", json={"answer": INSUFFICIENT_CONTEXT_ANSWER})
    assert response.json() == {"segments": [{"text": INSUFFICIENT_CONTEXT_ANSWER, "language": "pl"}]}
    assert not runtime.planner.calls


@pytest.mark.parametrize("body", [{"answer": ""}, {"answer": " \n"}, {"answer": "x" * 6001},
                                     {"answer": 123}, {}, {"answer": "x", "citations": []}])
def test_invalid_speech_request_is_safe_before_models(body):
    runtime = SpeechTransportRuntime([])
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        response = client.post("/api/assistant/speech-plan", json=body)
    assert response.status_code == 422 and response.json() == {"detail": "Invalid speech request."}
    assert not runtime.answers and not runtime.planner.calls


@pytest.mark.parametrize("failure", ["rewrite", "language", "exception", "unavailable"])
def test_speech_failures_safe_and_preserve_assistant_transport(caplog, failure):
    sentinel = "PRIVATE_ANSWER_PROMPT_PASSWORD"
    runtime = SpeechTransportRuntime([("changed", "pl")])
    if failure == "language":
        runtime.planner.output = {"segments": [{"text": sentinel, "language": "de"}]}
    if failure in ("exception", "unavailable"):
        async def fail(**kwargs):
            if failure == "unavailable":
                raise ServerRuntimeError()
            raise RuntimeError(sentinel)
        runtime.plan_speech = fail
    with TestClient(create_app(voice_runtime_factory=lambda: runtime)) as client:
        response = client.post("/api/assistant/speech-plan", json={"answer": sentinel})
        assert response.status_code == (503 if failure == "unavailable" else 500)
        assert response.json() == {"detail": "Memory assistant is unavailable." if failure == "unavailable"
                                  else "Speech planning could not be completed."}
        assert client.post("/api/assistant/ask", json={"query": "synthetic"}).status_code == 200
    assert sentinel not in caplog.text and sentinel not in response.text


def test_production_runtime_reuses_rag_qwen_lane_without_sessions_or_other_models():
    async def scenario():
        h = Harness()
        llm = PlannerLLM(CASES[0])
        async def plan(**kwargs):
            return await plan_speech(provider=llm, **kwargs)
        h.rag.plan_speech = plan
        runtime = h.runtime()
        await runtime.startup()
        before_sessions = len(h.sessions.sessions)
        for _ in range(2):
            result = await runtime.plan_speech(answer=CASES[0][0][0])
            assert result.original_answer == CASES[0][0][0]
        await runtime.plan_speech(answer=INSUFFICIENT_CONTEXT_ANSWER)
        assert len(llm.calls) == 2
        assert not h.bge.requests and not h.whisper.requests
        assert len(h.sessions.sessions) == before_sessions
        assert h.counts == dict(whisper=1, bge=1, qwen_rag=1, engine=1)
        await runtime.shutdown()
        assert h.rag.closed == h.bge.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


def test_speech_planning_shares_model_lane_with_rag_and_shutdown_cancels():
    async def scenario():
        h, probe = Harness(), Probe()
        llm = PlannerLLM(CASES[0])
        async def plan(**kwargs):
            await probe.run("speech")
            return await plan_speech(provider=llm, **kwargs)
        h.rag.plan_speech = plan
        h.rag.before = lambda: probe.run("rag")
        runtime = h.runtime()
        await runtime.startup()
        speech = asyncio.create_task(runtime.plan_speech(answer=CASES[0][0][0]))
        assert await probe.started.get() == "speech"
        rag = asyncio.create_task(runtime.ask_memory(query="synthetic"))
        await probe.release.put(None)
        await speech
        assert await probe.started.get() == "rag" and probe.maximum == 1
        await runtime.shutdown()
        with pytest.raises(asyncio.CancelledError):
            await rag
        assert not runtime._ai_callers and not runtime._inference_tasks
    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_speech_http_disconnect_cancels_planner_no_response_and_runtime_reusable():
    async def scenario():
        h = Harness()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        h.rag.plan_speech = blocked
        runtime = h.runtime()
        app = create_app(voice_runtime_factory=lambda: runtime)
        async with app.router.lifespan_context(app):
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            await incoming.put(dict(type="http.request", body=json.dumps({"answer": "synthetic"}).encode(), more_body=False))
            scope = dict(type="http", asgi={"version": "3.0"}, method="POST", scheme="http",
                path="/api/assistant/speech-plan", raw_path=b"/api/assistant/speech-plan", root_path="",
                query_string=b"", http_version="1.1", headers=[(b"content-type", b"application/json")],
                server=("127.0.0.1", 8000), client=("127.0.0.1", 5000))
            request = asyncio.create_task(app(scope, incoming.get, outgoing.put))
            await entered.wait()
            await incoming.put(dict(type="http.disconnect"))
            await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 2)
            assert cancelled.is_set()
            # Core logging middleware may convert disconnected ASGI cancellation
            # to its own safe error; it must never emit a successful speech plan.
            assert not any(m.get("status") == 200 for m in outgoing._queue)
            assert not runtime._ai_callers and not runtime._inference_tasks
            await runtime.ask_memory(query="synthetic next request")
    asyncio.run(asyncio.wait_for(scenario(), 10))
