"""Speech HTTP first request and real TCP disconnect through production router/lane."""
import asyncio
import json

import httpx

from backend.tests.integration.test_assistant_uvicorn import worker


def test_real_uvicorn_speech_success_disconnect_and_next_assistant_request():
    async def scenario():
        async with worker() as (process, ready):
            port = ready["port"]
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
                answer = "To jest syntetyczna odpowiedź."
                response = await client.post("/api/assistant/speech-plan", json={"answer": answer})
                assert response.status_code == 200 and response.json() == {"segments": [{"text": answer, "language": "pl"}]}
                await client.post("/_test/slow-next")
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                body = json.dumps({"answer": answer}).encode()
                writer.write(b"POST /api/assistant/speech-plan HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
                await process.event("model_blocked")
                writer.close(); await writer.wait_closed()
                # Next ask waits on the same lane; it can complete only after cancellation releases it.
                response = await client.post("/api/assistant/ask", json={"query": "synthetic next"})
                assert response.status_code == 200
                stats = (await client.get("/_test/stats")).json()
                assert stats["counts"]["cancelled"] == 1
                assert stats["checkedout"] == stats["ai_tasks"] == stats["ai_callers"] == 0
                assert stats["counts"]["bge_clients"] == stats["counts"]["qwen_clients"] == 1
                await client.post("/_test/shutdown")
    asyncio.run(asyncio.wait_for(scenario(), 60))
