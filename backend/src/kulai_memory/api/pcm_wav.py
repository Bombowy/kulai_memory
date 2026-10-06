"""Owned, bounded PCM-to-WAV streaming for WebSocket recordings."""

from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path
from typing import Any


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
MAX_RECORDING_SECONDS = 10 * 60
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES
MAX_TOTAL_AUDIO_BYTES = BYTES_PER_SECOND * MAX_RECORDING_SECONDS
MAX_BINARY_FRAME_BYTES = 256 * 1024


class PcmFrameError(ValueError):
    """A binary frame cannot be accepted as PCM16."""


class PcmAudioLimitError(PcmFrameError):
    """A frame or complete recording exceeds its configured limit."""


class OwnedPcmWav:
    """Write only adapter-created PCM data to one owned temporary WAV."""

    def __init__(self) -> None:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="kulai-memory-ws-",
            suffix=".wav",
        )
        os.close(descriptor)
        self._path = Path(raw_path)
        self._stream: Any = None
        self._byte_count = 0
        self._finished = False
        try:
            stream = wave.open(str(self._path), "wb")
            stream.setnchannels(CHANNELS)
            stream.setsampwidth(SAMPLE_WIDTH_BYTES)
            stream.setframerate(SAMPLE_RATE)
            self._stream = stream
        except Exception:
            self._path.unlink(missing_ok=True)
            raise

    @property
    def path(self) -> Path:
        return self._path

    @property
    def byte_count(self) -> int:
        return self._byte_count

    def write(self, data: bytes) -> None:
        if self._finished or self._stream is None:
            raise PcmFrameError
        if len(data) > MAX_BINARY_FRAME_BYTES:
            raise PcmAudioLimitError
        if len(data) % SAMPLE_WIDTH_BYTES:
            raise PcmFrameError
        if self._byte_count + len(data) > MAX_TOTAL_AUDIO_BYTES:
            raise PcmAudioLimitError
        self._stream.writeframesraw(data)
        self._byte_count += len(data)

    def finish(self) -> Path:
        if not self._finished:
            stream = self._stream
            self._stream = None
            self._finished = True
            if stream is not None:
                stream.close()
        return self._path

    def cleanup(self) -> None:
        try:
            self.finish()
        finally:
            self._path.unlink(missing_ok=True)


__all__ = [
    "BYTES_PER_SECOND",
    "CHANNELS",
    "MAX_BINARY_FRAME_BYTES",
    "MAX_RECORDING_SECONDS",
    "MAX_TOTAL_AUDIO_BYTES",
    "OwnedPcmWav",
    "PcmAudioLimitError",
    "PcmFrameError",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH_BYTES",
]
