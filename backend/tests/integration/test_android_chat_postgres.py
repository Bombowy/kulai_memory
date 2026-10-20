"""Run the Android JVM suite through the existing synthetic SECURE/owned-DB fixture."""
import asyncio
import os
from pathlib import Path

import pytest

from backend.tests.integration.test_conversation_api_postgres import api
from kulai_memory.database_safety import database_snapshot_url, require_owned_database


def test_android_chat_actual_http_ws_restart_and_idempotency(monkeypatch):
    if os.environ.get("KULAI_RUN_ANDROID_INTEGRATION") != "1":
        pytest.skip("Enable Android integration; requires local Android SDK and the existing Gradle wrapper.")

    async def run():
        async with api(monkeypatch) as h:
            await require_owned_database(h.owned, config=h.config)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("DB_", "KULAI_")) and key != "DATABASE_URL"}
            environment.update(KULAI_ANDROID_CHAT_TEST_ORIGIN=h.url,
                KULAI_ANDROID_CHAT_TEST_DEVICE=str(h.credential.device_id),
                KULAI_ANDROID_CHAT_TEST_BEARER=h.credential.access_token, KULAI_ANDROID_CHAT_TEST_OWNED="1")
            environment.setdefault("ANDROID_HOME", str(Path(os.environ["LOCALAPPDATA"]) / "Android/sdk"))
            directory = Path(__file__).resolve().parents[3] / "mobile/android"
            process = await asyncio.create_subprocess_exec("cmd.exe", "/d", "/c", "gradlew.bat",
                "testDebugUnitTest", "assembleDebug", "--console=plain", "--rerun-tasks",
                cwd=directory, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 240)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
            safe = output.decode("utf-8", errors="replace").replace(h.credential.access_token, "[redacted]")
            evidence = Path(os.environ["LOCALAPPDATA"]) / "KulAI-Memory-Tests/v2.5-20261021"
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "android-gradle.log").write_text(safe, encoding="utf-8")
            assert process.returncode == 0, safe[-8000:]
            # This assertion prevents a skipped/up-to-date Android test from looking like a PASS.
            snapshot = await database_snapshot_url(h.target.async_url)
            assert snapshot.conversations.conversations.count == 2
            assert snapshot.conversations.turns.count == 3 and snapshot.conversations.messages.count == 6
            assert snapshot.memories.count == snapshot.vectors.count == 0
            assert h.llm.attempts == 3 and len(h.runtime._provider.requests) == 1
    asyncio.run(asyncio.wait_for(run(), 300))
