"""Real TCP/real Uvicorn processes; never query private main in automated tests."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager

import httpx
import pytest

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_retrieval_postgres import _seed_memories
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from kulai_memory.application import Memory
from uuid import UUID


class Worker:
    def __init__(self, process):
        self.process = process
        self.lines = []

    async def event(self, kind):
        while True:
            raw = await asyncio.wait_for(self.process.stdout.readline(), 60)
            assert raw, "Test Uvicorn worker stopped before its structural event."
            line = raw.decode("utf-8", errors="replace").strip()
            self.lines.append(line)
            if line.startswith("TEST_EVENT="):
                event = json.loads(line.split("=", 1)[1])
                if event["kind"] == kind:
                    return event


@asynccontextmanager
async def worker(*, database=None, launcher=False, direct=False):
    env = dict(os.environ)
    env.pop("KULAI_TEST_UVICORN_DATABASE", None)
    if database:
        env["KULAI_TEST_UVICORN_DATABASE"] = database
    env["PYTHONUNBUFFERED"] = "1"
    args = [sys.executable, "-m", "backend.tests.uvicorn_assistant_worker"]
    if direct:
        args += ["--direct"]
    if launcher:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        args += ["--dev-launcher", "--port", str(port)]
    process = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.STDOUT)
    value = Worker(process)
    stopped = False
    try:
        ready = None if direct else await value.event("ready")
        try:
            yield value, ready
        finally:
            if ready is not None and process.returncode is None:
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(f"http://127.0.0.1:{ready['port']}/_test/shutdown")
                except httpx.HTTPError:
                    pass  # The body may already have requested clean shutdown.
            stopped_event = await value.event("stopped")
            assert stopped_event["counts"]["bge_closed"] == stopped_event["counts"]["qwen_closed"] == 1
            assert stopped_event["ai_callers"] == stopped_event["ai_tasks"] == 0
            stopped = True
    finally:
        if process.returncode is None:
            # For --dev-launcher the resource-owning child has already closed;
            # the reload supervisor is idle and can now be terminated.
            process.terminate()
        await asyncio.wait_for(process.communicate(), 15)
        assert stopped, "The test worker did not complete resource shutdown."


async def requests(database=None, launcher=False, factory=None):
    async with worker(database=database, launcher=launcher) as (process, ready):
        assert ready["counts"]["qwen_clients"] == ready["counts"]["bge_clients"] == 1
        assert ready["counts"]["qwen_requests"] == 0
        baseline = await snapshot(factory) if factory else None
        url = f"http://127.0.0.1:{ready['port']}"
        async with httpx.AsyncClient(base_url=url, timeout=200,
                                     headers={"Connection": "close"}) as client:
            for number in (1, 2):
                response = await client.post("/api/assistant/ask", json={
                    "query": "Gdzie mieszka syntetyczny zielony smok?", "top_k": 5})
                assert response.status_code == 200
                body = response.json()
                assert body["status"] == "answered" and body["citations"]
                if database:
                    assert [c["memory_id"] for c in body["citations"]] == [str(UUID(int=42))]
                    assert "venus" in body["answer"].lower() or "wenus" in body["answer"].lower()
                stats = (await client.get("/_test/stats")).json()
                assert stats["counts"]["bge_clients"] == stats["counts"]["qwen_clients"] == 1
                assert stats["counts"]["answered"] == number
                assert stats["checkedout"] == stats["ai_tasks"] == stats["ai_callers"] == 0
                print(f"uvicorn.first={number == 1};http=200;exact_synthetic_citation=true;clients=1/1;checkout=0")
            if factory:
                assert await snapshot(factory) == baseline
            await client.post("/_test/shutdown")


@pytest.mark.parametrize("launcher", [False, True], ids=["uvicorn", "scripts_dev_reload"])
def test_real_tcp_first_second_ask_controlled_runtime(launcher):
    asyncio.run(asyncio.wait_for(requests(launcher=launcher), 120))


def test_real_tcp_owned_bge_qwen_first_second_ask():
    _require_opt_in()
    if not all(os.environ.get(k) == "1" for k in ("KULAI_RUN_OLLAMA_INTEGRATION", "KULAI_RUN_LLM_INTEGRATION")):
        pytest.skip("Enable BGE/Qwen opt-ins for owned real Uvicorn RAG.")
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            await _seed_memories(factory, (Memory(id=UUID(int=42), content="The synthetic green dragon lives on Venus."),))
            # Only vector indexing by startup is expected; canonical Memory never changes.
            before = await snapshot(factory)
            await requests(database=factory.kw["bind"].url.database, launcher=True, factory=factory)
            after = await snapshot(factory)
            assert before.memories == after.memories and before.tombstones == after.tombstones
            assert after.vectors.count == 1
    asyncio.run(asyncio.wait_for(scenario(), 600))


def test_direct_owned_production_server_runtime_real_bge_qwen():
    _require_opt_in()
    if not all(os.environ.get(k) == "1" for k in ("KULAI_RUN_OLLAMA_INTEGRATION", "KULAI_RUN_LLM_INTEGRATION")):
        pytest.skip("Enable BGE/Qwen for direct production server isolation.")
    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            await _seed_memories(factory, (Memory(id=UUID(int=42), content="The synthetic green dragon lives on Venus."),))
            async with worker(database=factory.kw["bind"].url.database, direct=True) as (process, ready):
                result = await process.event("direct")
                assert result["sufficient"] and result["citation_exact"] and result["venus"]
                assert result["counts"]["bge_clients"] == result["counts"]["qwen_clients"] == 1
                assert result["counts"]["qwen_requests"] == 1 and result["checkedout"] == 0
    asyncio.run(asyncio.wait_for(scenario(), 300))


def test_real_tcp_disconnect_cancels_slow_rag_then_next_request_works():
    async def scenario():
        async with worker() as (process, ready):
            url = f"http://127.0.0.1:{ready['port']}"
            async with httpx.AsyncClient(base_url=url, timeout=20) as client:
                await client.post("/_test/slow-next")
                reader, writer = await asyncio.open_connection("127.0.0.1", ready["port"])
                body = json.dumps({"query": "synthetic slow question"}).encode()
                writer.write(b"POST /api/assistant/ask HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
                await process.event("model_blocked")
                writer.close()
                await writer.wait_closed()
                done = await process.event("operation_done")
                assert done["cancelled"]
                assert done["checkedout"] == done["ai_callers"] == done["ai_tasks"] == 0
                response = await client.post("/api/assistant/ask", json={"query": "synthetic next question"})
                assert response.status_code == 200
                stats = (await client.get("/_test/stats")).json()
                assert stats["counts"]["cancelled"] == 1 and stats["counts"]["answered"] == 1
                assert stats["counts"]["bge_clients"] == stats["counts"]["qwen_clients"] == 1
                await client.post("/_test/shutdown")
    asyncio.run(asyncio.wait_for(scenario(), 90))


@pytest.mark.parametrize("encoding", ["string_utf8", "bytes_utf8", "legacy_invalid"])
def test_first_request_windows_invoke_rest_method(encoding):
    if os.name != "nt":
        pytest.skip("This client regression uses Windows PowerShell Invoke-RestMethod.")
    async def scenario():
        async with worker(launcher=True) as (process, ready):
            script = """
$body = @{query='Gdzie mieszka mój syntetyczny zielony smok?';top_k=5} | ConvertTo-Json
if ($env:KULAI_TEST_ENCODING -eq 'bytes_utf8') {
    $body = [Text.Encoding]::UTF8.GetBytes($body)
    $contentType = 'application/json'
} elseif ($env:KULAI_TEST_ENCODING -eq 'string_utf8') {
    $contentType = 'application/json; charset=utf-8'
} else { $contentType = 'application/json' }
try {
    $result = Invoke-RestMethod -Uri $env:KULAI_TEST_URL -Method Post -ContentType $contentType -Body $body -DisableKeepAlive
    @{status=200;answered=($result.status -eq 'answered');citations=$result.citations.Count} | ConvertTo-Json -Compress
} catch {
    @{status=[int]$_.Exception.Response.StatusCode;answered=$false;citations=0} | ConvertTo-Json -Compress
}
"""
            env = dict(os.environ, KULAI_TEST_URL=f"http://127.0.0.1:{ready['port']}/api/assistant/ask",
                       KULAI_TEST_ENCODING=encoding)
            client = await asyncio.create_subprocess_exec("powershell.exe", "-NoProfile", "-NonInteractive",
                "-Command", script, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, _ = await asyncio.wait_for(client.communicate(), 30)
            result = json.loads(output)
            if encoding == "legacy_invalid":
                async with httpx.AsyncClient() as diagnostic:
                    stats = (await diagnostic.get(f"http://127.0.0.1:{ready['port']}/_test/stats")).json()
                assert result == dict(status=400, answered=False, citations=0)
                assert stats["counts"]["asks"] == 0
                print("uvicorn.invalid_utf8=400;ai_asks=0")
            else:
                assert result == dict(status=200, answered=True, citations=1)
    asyncio.run(asyncio.wait_for(scenario(), 90))
