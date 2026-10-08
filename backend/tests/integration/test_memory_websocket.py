from __future__ import annotations

import asyncio
import os
import wave
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from kulai_transcription import (
    AudioInputKind,
    TranscriptionCapabilities,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.websockets import WebSocketDisconnect

from kulai_memory.app_factory import create_app
from kulai_memory.application import TranscriptMemoryIngestionStatus
from kulai_memory.database_safety import (
    async_database_url,
    create_owned_temporary_database,
    database_config,
    database_config_for_database,
    database_host_is_loopback,
    drop_owned_temporary_database,
)
from kulai_memory.server import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings, get_settings
from scripts import migrate
from kulai_memory.automatic_indexing import AutomaticMemoryIndexer
from backend.tests.integration.test_automatic_indexing_postgres import CheckedProvider


def _require_postgres() -> None:
    if os.environ.get("KULAI_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_POSTGRES_INTEGRATION=1 for real PostgreSQL.")
    if get_settings().app_env.lower() not in {
        "dev",
        "development",
        "local",
        "test",
    }:
        pytest.fail("WebSocket integration requires a non-production APP_ENV.")
    if not database_host_is_loopback(database_config()):
        pytest.fail("WebSocket integration requires loopback PostgreSQL.")


def _upgrade_database(url: str) -> None:
    previous_url = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        config = migrate.alembic_config()
        assert migrate.configure_x_arguments(config)
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()


class FixedProvider:
    provider_id = "websocket-postgres-fake"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.PATH}),
        modes=frozenset({TranscriptionMode.TRANSCRIBE}),
    )

    def __init__(self) -> None:
        self.call_count = 0

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.call_count += 1
        return TranscriptionResult(
            text="durable websocket transcript",
            provider_id=self.provider_id,
            model_id="large-v3",
            mode=request.mode,
            duration_seconds=0.1,
        )


class RecordingRuntime(VoiceMemoryServerRuntime):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ingestion_statuses: list[TranscriptMemoryIngestionStatus] = []

    async def ingest(self, **kwargs: Any):
        result = await super().ingest(**kwargs)
        self.ingestion_statuses.append(result.status)
        return result


def _start(ingestion_id: UUID) -> dict[str, object]:
    return {
        "schema_version": 1,
        "type": "recording.start",
        "ingestion_id": str(ingestion_id),
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
        },
    }


def _run_connection(
    client: TestClient,
    *,
    ingestion_id: UUID,
    chunks: Iterable[bytes],
) -> list[dict[str, Any]]:
    with client.websocket_connect("/ws/memory") as websocket:
        websocket.send_json(_start(ingestion_id))
        events = [websocket.receive_json()]
        for chunk in chunks:
            websocket.send_bytes(chunk)
        websocket.send_json({"schema_version": 1, "type": "recording.stop"})
        while True:
            try:
                events.append(websocket.receive_json())
            except WebSocketDisconnect as exc:
                assert exc.code == 1000
                break
    return events


async def _counts(url: str) -> tuple[int, int]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            memories = await connection.scalar(text("SELECT count(*) FROM memories"))
            vectors = await connection.scalar(
                text("SELECT count(*) FROM kulai_vector_records")
            )
            await connection.rollback()
        return int(memories or 0), int(vectors or 0)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("index_failure", [False, True])
def test_websocket_durable_duplicate_uses_real_postgres(index_failure) -> None:
    _require_postgres()
    main_config = database_config()
    owned = asyncio.run(
        create_owned_temporary_database(kind="backup", config=main_config)
    )
    owned_url = async_database_url(database=owned.name, config=main_config)
    try:
        _upgrade_database(owned_url)
        provider = FixedProvider()
        provider_factory_calls = 0
        runtime: RecordingRuntime | None = None
        embedding_provider = None

        def indexer_factory(**kwargs):
            nonlocal embedding_provider
            embedding_provider = CheckedProvider(kwargs["session_factory"].kw["bind"])
            if index_failure:
                embedding_provider.error = RuntimeError("PRIVATE_EMBED_SENTINEL")
            return AutomaticMemoryIndexer(**kwargs, provider_factory=lambda **ignored: embedding_provider)

        def provider_factory(settings: Settings) -> FixedProvider:
            nonlocal provider_factory_calls
            provider_factory_calls += 1
            assert settings.kulai_whisper_model == "large-v3"
            assert settings.kulai_whisper_device == "cuda"
            assert settings.kulai_whisper_compute_type == "int8_float16"
            assert settings.kulai_whisper_vad_filter is True
            return provider

        def runtime_factory() -> RecordingRuntime:
            nonlocal runtime
            runtime = RecordingRuntime(
                settings=Settings(_env_file=None, kulai_vector_dimension=1024),
                provider_factory=provider_factory,
                database_config_factory=lambda: database_config_for_database(
                    owned.name,
                    config=main_config,
                ),
                indexer_factory=indexer_factory,
            )
            return runtime

        app = create_app(voice_runtime_factory=runtime_factory)
        ingestion_id = uuid4()
        with TestClient(app) as client:
            if index_failure:
                with client.websocket_connect("/ws/memory") as websocket:
                    websocket.send_json(_start(ingestion_id))
                    first = [websocket.receive_json()]
                    websocket.send_bytes(b"\0\0")
                    websocket.send_json({"schema_version": 1, "type": "recording.stop"})
                    first.extend(websocket.receive_json() for _ in range(3))
                    assert first[-1]["payload"]["code"] == "memory.index_failed"
                    assert first[-1]["payload"]["recoverable"] is True
                    assert asyncio.run(_counts(owned_url)) == (1, 0)
                    embedding_provider.error = None
                    websocket.send_json({"schema_version": 1, "type": "memory.retry"})
                    first.extend(websocket.receive_json() for _ in range(2))
                    with pytest.raises(WebSocketDisconnect) as close:
                        websocket.receive_json()
                    assert close.value.code == 1000
                    assert provider.call_count == 1
                    assert [event["sequence"] for event in first] == list(range(1, 7))
            else:
                first = _run_connection(client, ingestion_id=ingestion_id, chunks=[b"\x00\x00"])
            previous_calls = embedding_provider.calls
            second = _run_connection(
                client,
                ingestion_id=ingestion_id,
                chunks=[b"\x00\x00"],
            )
            assert embedding_provider.calls == previous_calls

        first_saved = next(event for event in first if event["type"] == "memory.saved")
        second_saved = next(event for event in second if event["type"] == "memory.saved")
        assert first_saved["payload"]["memory_id"] == second_saved["payload"]["memory_id"]
        assert first[0]["session_id"] == second[0]["session_id"]
        assert runtime is not None
        assert runtime.ingestion_statuses == (
            [TranscriptMemoryIngestionStatus.DUPLICATE] if index_failure else
            [TranscriptMemoryIngestionStatus.CREATED]
        ) + [TranscriptMemoryIngestionStatus.DUPLICATE]
        assert provider.call_count == 2
        assert provider_factory_calls == 1
        assert asyncio.run(_counts(owned_url)) == (1, 1)
        assert embedding_provider.closed == 1
    finally:
        asyncio.run(drop_owned_temporary_database(owned, config=main_config))


def _compatible_explicit_audio() -> tuple[Path | None, str]:
    configured = os.environ.get("KULAI_WHISPER_AUDIO")
    if not configured:
        return None, "not_configured"
    path = Path(configured)
    if not path.is_file():
        return None, "not_a_file"
    try:
        with wave.open(str(path), "rb") as stream:
            compatible = (
                stream.getframerate() == 16000
                and stream.getnchannels() == 1
                and stream.getsampwidth() == 2
                and stream.getcomptype() == "NONE"
            )
    except (OSError, wave.Error):
        return None, "not_pcm_wav"
    if not compatible:
        return None, "incompatible_format"
    return path, "compatible"


def _wav_chunks(path: Path) -> Iterator[bytes]:
    with wave.open(str(path), "rb") as stream:
        while chunk := stream.readframes(8192):
            yield chunk


def test_real_large_v3_silence_through_websocket_reuses_model() -> None:
    _require_postgres()
    if os.environ.get("KULAI_RUN_WHISPER_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_WHISPER_INTEGRATION=1 for real Whisper.")
    main_config = database_config()
    owned = asyncio.run(
        create_owned_temporary_database(kind="backup", config=main_config)
    )
    owned_url = async_database_url(database=owned.name, config=main_config)
    try:
        _upgrade_database(owned_url)
        settings = Settings()
        assert settings.kulai_whisper_model == "large-v3"
        assert settings.kulai_whisper_device == "cuda"
        assert settings.kulai_whisper_compute_type == "int8_float16"
        assert settings.kulai_whisper_vad_filter is True
        provider_factory_calls = 0
        provider: Any = None
        runtime: VoiceMemoryServerRuntime | None = None

        def provider_factory(active_settings: Settings):
            nonlocal provider_factory_calls, provider
            from kulai_memory.whisper_provider import (
                create_whisper_transcription_provider,
            )

            provider_factory_calls += 1
            provider = create_whisper_transcription_provider(
                settings=active_settings
            )
            return provider

        def runtime_factory() -> VoiceMemoryServerRuntime:
            nonlocal runtime
            runtime = VoiceMemoryServerRuntime(
                settings=settings,
                provider_factory=provider_factory,
                database_config_factory=lambda: database_config_for_database(
                    owned.name,
                    config=main_config,
                ),
            )
            return runtime

        app = create_app(voice_runtime_factory=runtime_factory)
        silence = [b"\x00\x00" * 16000]
        expected_memories = 0
        with TestClient(app) as client:
            first = _run_connection(client, ingestion_id=uuid4(), chunks=silence)
            assert [event["type"] for event in first] == [
                "session.ready",
                "transcript.final",
            ]
            assert first[-1]["payload"]["text"].strip() == ""
            assert provider is not None
            loaded_model = getattr(provider, "_model", None)
            assert loaded_model is not None

            second = _run_connection(client, ingestion_id=uuid4(), chunks=silence)
            assert [event["type"] for event in second] == [
                "session.ready",
                "transcript.final",
            ]
            assert second[-1]["payload"]["text"].strip() == ""
            assert getattr(provider, "_model", None) is loaded_model

            explicit, explicit_status = _compatible_explicit_audio()
            if explicit is not None:
                speech = _run_connection(
                    client,
                    ingestion_id=uuid4(),
                    chunks=_wav_chunks(explicit),
                )
                final = next(event for event in speech if event["type"] == "transcript.final")
                saved = next(event for event in speech if event["type"] == "memory.saved")
                assert final["payload"]["text"].strip()
                assert UUID(saved["payload"]["memory_id"])
                expected_memories = 1
                print(
                    "websocket_explicit_speech status=created "
                    f"char_count={len(final['payload']['text'])}"
                )
            else:
                print(f"websocket_explicit_speech status={explicit_status}")

        assert runtime is not None
        assert provider_factory_calls == 1
        assert asyncio.run(_counts(owned_url)) == (expected_memories, 0)
        print(
            "websocket_silence statuses=empty,empty model=large-v3 "
            "device=cuda compute_type=int8_float16 vad_filter=true "
            "provider_reused=true model_reused=true"
        )
    finally:
        asyncio.run(drop_owned_temporary_database(owned, config=main_config))
