from __future__ import annotations

import json
import wave
from uuid import uuid4

import pytest

from kulai_memory.api.memory_ws_protocol import (
    MemoryRetryCommand,
    ProtocolMessageError,
    RecordingStartCommand,
    RecordingStopCommand,
    parse_client_command,
)
from kulai_memory.api.pcm_wav import (
    MAX_BINARY_FRAME_BYTES,
    MAX_TOTAL_AUDIO_BYTES,
    OwnedPcmWav,
    PcmAudioLimitError,
    PcmFrameError,
)


def _start_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "type": "recording.start",
        "ingestion_id": str(uuid4()),
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
        },
    }
    payload.update(updates)
    return payload


def test_protocol_parses_all_valid_commands() -> None:
    start = parse_client_command(json.dumps(_start_payload()))
    stop = parse_client_command(
        json.dumps({"schema_version": 1, "type": "recording.stop"})
    )
    retry = parse_client_command(
        json.dumps({"schema_version": 1, "type": "memory.retry"})
    )

    assert isinstance(start, RecordingStartCommand)
    assert isinstance(stop, RecordingStopCommand)
    assert isinstance(retry, MemoryRetryCommand)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps(_start_payload(ingestion_id="not-a-uuid")),
        json.dumps(_start_payload(schema_version=2)),
        json.dumps({"schema_version": 1, "type": "unknown"}),
        json.dumps(
            _start_payload(
                audio={
                    "encoding": "wav",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        ),
        json.dumps(
            _start_payload(
                audio={
                    "encoding": "pcm_s16le",
                    "sample_rate": 8000,
                    "channels": 1,
                }
            )
        ),
        json.dumps(
            _start_payload(
                audio={
                    "encoding": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 2,
                }
            )
        ),
        json.dumps(
            {
                "schema_version": 1,
                "type": "recording.stop",
                "unexpected": True,
            }
        ),
    ],
)
def test_protocol_rejects_invalid_messages_without_echoing_input(payload: str) -> None:
    with pytest.raises(ProtocolMessageError) as caught:
        parse_client_command(payload)

    assert payload not in str(caught.value)


def test_owned_wav_streams_pcm16_with_canonical_properties() -> None:
    owned = OwnedPcmWav()
    path = owned.path
    try:
        owned.write(b"\x01\x00" * 160)
        assert owned.byte_count == 320
        owned.finish()
        with wave.open(str(path), "rb") as stream:
            assert stream.getframerate() == 16000
            assert stream.getnchannels() == 1
            assert stream.getsampwidth() == 2
            assert stream.getnframes() == 160
    finally:
        owned.cleanup()
    assert not path.exists()


def test_owned_wav_rejects_odd_oversized_and_total_overflow_frames() -> None:
    odd = OwnedPcmWav()
    try:
        with pytest.raises(PcmFrameError):
            odd.write(b"x")
    finally:
        odd.cleanup()

    oversized = OwnedPcmWav()
    try:
        with pytest.raises(PcmAudioLimitError):
            oversized.write(b"x" * (MAX_BINARY_FRAME_BYTES + 2))
    finally:
        oversized.cleanup()

    total = OwnedPcmWav()
    try:
        full_chunk = b"\x00\x00" * (MAX_BINARY_FRAME_BYTES // 2)
        for _ in range(MAX_TOTAL_AUDIO_BYTES // len(full_chunk)):
            total.write(full_chunk)
        remainder = MAX_TOTAL_AUDIO_BYTES - total.byte_count
        if remainder:
            total.write(b"\x00" * remainder)
        with pytest.raises(PcmAudioLimitError):
            total.write(b"\x00\x00")
    finally:
        path = total.path
        total.cleanup()
    assert not path.exists()


def test_owned_wav_cleanup_is_idempotent() -> None:
    owned = OwnedPcmWav()
    path = owned.path
    owned.cleanup()
    owned.cleanup()
    assert not path.exists()
