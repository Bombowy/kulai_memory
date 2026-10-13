"""Test-only subprocess host: production app/runtime on real loopback TCP.

Control endpoints and safe counters exist only in this worker, never in the app
factory. An owned database is supplied by the parent; the worker never drops it.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from uuid import UUID

import uvicorn
from kulai_provider_ollama import provider as llm_module
from kulai_provider_ollama_embeddings import provider as bge_module

from backend.tests.test_server_assistant_runtime import Harness
from backend.tests.test_server_transport import answered
from backend.tests.test_desktop_controller import FakeProvider
from kulai_memory.app_factory import create_app
from kulai_memory.database_safety import OWNED_DATABASE_PREFIXES, database_config_for_database
from kulai_memory.server.runtime import VoiceMemoryServerRuntime
from kulai_memory.settings import get_settings


def emit(kind, **fields):
    print("TEST_EVENT=" + json.dumps(dict(kind=kind, **fields)), flush=True)


class State:
    def __init__(self):
        self.counts = dict(bge_clients=0, bge_requests=0, bge_closed=0,
                           qwen_clients=0, qwen_requests=0, qwen_closed=0, whisper=0,
                           asks=0, answered=0, cancelled=0)
        self.slow_next = False
        self.runtime = None
        self.harness = None

    def snapshot(self):
        r = self.runtime
        checkedout = (r._engine.pool.checkedout() if r._engine is not None
                      and hasattr(r._engine, "pool") else 0)
        counts = dict(self.counts)
        if self.harness:
            h = self.harness
            counts.update(bge_clients=h.counts["bge"], bge_closed=h.bge.closed,
                          qwen_clients=h.counts["qwen_rag"], qwen_closed=h.rag.closed,
                          whisper=h.counts["whisper"])
        return dict(counts=counts, checkedout=checkedout, ai_callers=len(r._ai_callers),
                    ai_tasks=len(r._inference_tasks), started=r.started)

    async def before_generation(self):
        if self.slow_next:
            self.slow_next = False
            assert self.snapshot()["checkedout"] == 0
            emit("model_blocked")
            try:
                await asyncio.Event().wait()
            finally:
                self.counts["cancelled"] += 1


def build_runtime(state):
    database = os.environ.get("KULAI_TEST_UVICORN_DATABASE")
    if database:
        assert any(database.startswith(p) for p in OWNED_DATABASE_PREFIXES.values())
        class BGEClient(bge_module.AsyncClient):
            def __init__(self, *args, **kwargs):
                state.counts["bge_clients"] += 1
                super().__init__(*args, **kwargs)
            async def embed(self, *args, **kwargs):
                assert state.snapshot()["checkedout"] == 0 and kwargs.get("dimensions") is None
                state.counts["bge_requests"] += 1
                return await super().embed(*args, **kwargs)
            async def close(self):
                state.counts["bge_closed"] += 1
                await super().close()
        class QwenClient(llm_module.AsyncClient):
            def __init__(self, *args, **kwargs):
                state.counts["qwen_clients"] += 1
                super().__init__(*args, **kwargs)
            async def chat(self, *args, **kwargs):
                assert state.snapshot()["checkedout"] == 0
                state.counts["qwen_requests"] += 1
                await state.before_generation()
                return await super().chat(*args, **kwargs)
            async def close(self):
                state.counts["qwen_closed"] += 1
                await super().close()
        bge_module.AsyncClient, llm_module.AsyncClient = BGEClient, QwenClient
        def whisper(settings):
            state.counts["whisper"] += 1
            return FakeProvider(("synthetic",))
        runtime = VoiceMemoryServerRuntime(settings=get_settings(), provider_factory=whisper,
            database_config_factory=lambda: database_config_for_database(database))
    else:
        h = state.harness = Harness()
        answered(h)
        runtime = h.runtime()
        h.rag.before = state.before_generation
        async def controlled_speech(*, answer):
            from kulai_memory.application.speech import SpeechPlan, SpeechSegment, SpeechLanguage
            await state.before_generation()
            return SpeechPlan(answer, (SpeechSegment(answer, SpeechLanguage.PL),))
        h.rag.plan_speech = controlled_speech
    state.runtime = runtime
    original = runtime.ask_memory
    async def ask_memory(**kwargs):
        state.counts["asks"] += 1
        try:
            result = await original(**kwargs)
            state.counts["answered"] += 1
            return result
        finally:
            emit("operation_done", cancelled=asyncio.current_task().cancelling() > 0,
                 **state.snapshot())
    runtime.ask_memory = ask_memory
    return runtime


class TestServer(uvicorn.Server):
    __test__ = False
    async def _serve(self, sockets=None):
        self.state = State()
        runtime = build_runtime(self.state)
        app = create_app(voice_runtime_factory=lambda: runtime)
        @app.get("/_test/stats")
        async def stats():
            return self.state.snapshot()
        @app.post("/_test/slow-next")
        async def slow():
            self.state.slow_next = True
            return {"armed": True}
        @app.post("/_test/shutdown")
        async def shutdown():
            self.should_exit = True
            return {"stopping": True}
        self.config.app = app
        self.config.loaded = False
        try:
            await super()._serve(sockets=sockets)
        finally:
            emit("stopped", **self.state.snapshot())

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        assert self.started
        port = self.servers[0].sockets[0].getsockname()[1]
        emit("ready", port=port, **self.state.snapshot())


async def direct():
    state = State()
    runtime = build_runtime(state)
    try:
        await runtime.startup()
        assert state.counts["qwen_requests"] == 0
        result = await runtime.ask_memory(query="Gdzie mieszka syntetyczny zielony smok?", top_k=5)
        emit("direct", sufficient=result.sufficient_context,
             citation_exact=[c.memory_id for c in result.citations] == [UUID(int=42)],
             venus="venus" in result.answer.lower() or "wenus" in result.answer.lower(),
             **state.snapshot())
    finally:
        await runtime.shutdown()
        emit("stopped", **state.snapshot())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--dev-launcher", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.direct:
        assert os.environ.get("KULAI_TEST_UVICORN_DATABASE")
        asyncio.run(direct())
    elif args.dev_launcher:
        # Same launcher, host, reload supervisor and Windows subprocess loop.
        # Substitute only the app/runtime fixture in the server child before load.
        import sys
        from scripts import dev
        importlib.import_module("uvicorn.main").Server = TestServer
        sys.argv = ["scripts/dev.py", "--port", str(args.port)]
        dev.main()
    else:
        TestServer(uvicorn.Config("kulai_memory.main:app", host="127.0.0.1", port=args.port,
                                 access_log=False, log_level="warning")).run()


if __name__ == "__main__":
    main()
