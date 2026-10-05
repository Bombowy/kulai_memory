"""Bounded PCM microphone recording into owned temporary WAV files."""

from __future__ import annotations

import importlib
import operator
import os
import tempfile
import threading
import wave
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from .models import (
    DesktopDependencyError,
    DesktopRecordingError,
    DesktopStateError,
    MicrophoneDevice,
    RecordingArtifact,
)


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
DTYPE = "int16"
DEFAULT_MAX_DURATION_SECONDS = 600.0
DEFAULT_BLOCK_FRAMES = 1_600


class _RawInputStream(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def read(self, frames: int) -> tuple[object, bool]: ...


class _SoundDeviceBackend(Protocol):
    default: Any

    def query_devices(self) -> Sequence[dict[str, Any]]: ...
    def query_hostapis(self) -> Sequence[dict[str, Any]]: ...
    def check_input_settings(self, **kwargs: object) -> None: ...
    def RawInputStream(self, **kwargs: object) -> _RawInputStream: ...


def _load_sounddevice() -> _SoundDeviceBackend:
    try:
        module = importlib.import_module("sounddevice")
    except ModuleNotFoundError as exc:
        raise DesktopDependencyError from exc
    return cast(_SoundDeviceBackend, cast(ModuleType, module))


def _device_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        device_id = operator.index(value)
    except (TypeError, ValueError):
        return None
    return device_id if device_id >= 0 else None


def _default_input_device(value: object) -> int | None:
    scalar = _device_id(value)
    if scalar is not None:
        return scalar
    try:
        input_value = value[0]  # type: ignore[index]
    except Exception:
        return None
    return _device_id(input_value)


class MicrophoneRecorder:
    """Record one microphone stream at a time without retaining it in memory."""

    def __init__(
        self,
        *,
        sounddevice_backend: _SoundDeviceBackend | None = None,
        temp_directory: Path | None = None,
        max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
        block_frames: int = DEFAULT_BLOCK_FRAMES,
        startup_timeout_seconds: float = 5.0,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        if max_duration_seconds <= 0 or block_frames <= 0:
            raise ValueError("Recording limits must be positive.")
        self._sounddevice = sounddevice_backend or _load_sounddevice()
        self._temp_directory = temp_directory
        self._max_frames = round(max_duration_seconds * SAMPLE_RATE)
        self._block_frames = block_frames
        self._startup_timeout_seconds = startup_timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._lock = threading.RLock()
        self._owned_paths: set[Path] = set()
        self._active_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._ready_event: threading.Event | None = None
        self._finished_event: threading.Event | None = None
        self._frame_count = 0
        self._limit_reached = False
        self._error: DesktopRecordingError | None = None

    def list_devices(self) -> tuple[MicrophoneDevice, ...]:
        try:
            raw_devices = tuple(self._sounddevice.query_devices())
            host_apis = tuple(self._sounddevice.query_hostapis())
        except Exception as exc:
            raise DesktopRecordingError from exc

        try:
            configured_default = self._sounddevice.default.device
        except Exception:
            default_input = None
        else:
            default_input = _default_input_device(configured_default)

        devices: list[MicrophoneDevice] = []
        for device_id, item in enumerate(raw_devices):
            if int(item.get("max_input_channels", 0)) < 1:
                continue
            host_api: str | None = None
            host_index = item.get("hostapi")
            if isinstance(host_index, int) and 0 <= host_index < len(host_apis):
                host_name = host_apis[host_index].get("name")
                if isinstance(host_name, str) and host_name.strip():
                    host_api = host_name.strip()
            raw_name = item.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else "Microphone"
            devices.append(
                MicrophoneDevice(
                    device_id=device_id,
                    name=name or "Microphone",
                    host_api=host_api,
                    is_default=device_id == default_input,
                )
            )
        return tuple(devices)

    def start(self, *, device_id: int) -> None:
        with self._lock:
            if self._active_path is not None:
                raise DesktopStateError
            try:
                self._sounddevice.check_input_settings(
                    device=device_id,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    samplerate=SAMPLE_RATE,
                )
            except Exception as exc:
                raise DesktopRecordingError from exc

            directory = str(self._temp_directory) if self._temp_directory else None
            descriptor, raw_path = tempfile.mkstemp(
                prefix="kulai-memory-recording-",
                suffix=".wav",
                dir=directory,
            )
            os.close(descriptor)
            path = Path(raw_path).resolve()
            self._owned_paths.add(path)
            self._active_path = path
            self._stop_event = threading.Event()
            self._ready_event = threading.Event()
            self._finished_event = threading.Event()
            self._frame_count = 0
            self._limit_reached = False
            self._error = None
            thread = threading.Thread(
                target=self._capture,
                args=(path, device_id),
                name="kulai-microphone-capture",
                daemon=True,
            )
            self._thread = thread
            ready = self._ready_event
            thread.start()

        if not ready.wait(self._startup_timeout_seconds):
            self._abort_start(path)
            raise DesktopRecordingError
        with self._lock:
            error = self._error
        if error is not None:
            self._abort_start(path)
            raise error

    def stop(self) -> RecordingArtifact:
        with self._lock:
            path = self._active_path
            thread = self._thread
            stop_event = self._stop_event
            if path is None or thread is None or stop_event is None:
                raise DesktopStateError
            stop_event.set()

        thread.join(self._stop_timeout_seconds)
        if thread.is_alive():
            raise DesktopRecordingError

        with self._lock:
            error = self._error
            duration = self._frame_count / SAMPLE_RATE
            limit_reached = self._limit_reached
            self._clear_active_state()

        if error is not None:
            self._cleanup_owned_path(path)
            raise error
        return RecordingArtifact(
            path=path,
            duration_seconds=duration,
            limit_reached=limit_reached,
        )

    def cleanup(self, artifact: RecordingArtifact) -> None:
        path = artifact.path.resolve()
        with self._lock:
            if path == self._active_path or path not in self._owned_paths:
                raise DesktopStateError
        self._cleanup_owned_path(path)

    def shutdown(self) -> None:
        artifact: RecordingArtifact | None = None
        with self._lock:
            active = self._active_path is not None
        if active:
            try:
                artifact = self.stop()
            except (DesktopRecordingError, DesktopStateError):
                pass
        if artifact is not None:
            self.cleanup(artifact)
        with self._lock:
            remaining = tuple(self._owned_paths)
        for path in remaining:
            self._cleanup_owned_path(path)

    def _capture(self, path: Path, device_id: int) -> None:
        stream: _RawInputStream | None = None
        ready = self._ready_event
        finished = self._finished_event
        stop_event = self._stop_event
        assert ready is not None and finished is not None and stop_event is not None
        try:
            with wave.open(str(path), "wb") as output:
                output.setnchannels(CHANNELS)
                output.setsampwidth(SAMPLE_WIDTH)
                output.setframerate(SAMPLE_RATE)
                stream = self._sounddevice.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=self._block_frames,
                    device=device_id,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    latency="high",
                )
                stream.start()
                ready.set()
                while not stop_event.is_set():
                    with self._lock:
                        remaining = self._max_frames - self._frame_count
                    if remaining <= 0:
                        with self._lock:
                            self._limit_reached = True
                        break
                    frames_to_read = min(self._block_frames, remaining)
                    data, overflowed = stream.read(frames_to_read)
                    if overflowed:
                        raise RuntimeError("input overflow")
                    frame_bytes = bytes(data)
                    expected_bytes = frames_to_read * CHANNELS * SAMPLE_WIDTH
                    frame_bytes = frame_bytes[:expected_bytes]
                    actual_frames = len(frame_bytes) // (CHANNELS * SAMPLE_WIDTH)
                    if actual_frames == 0:
                        continue
                    output.writeframesraw(frame_bytes)
                    with self._lock:
                        self._frame_count += actual_frames
                        if self._frame_count >= self._max_frames:
                            self._limit_reached = True
                            break
        except Exception as exc:
            with self._lock:
                self._error = DesktopRecordingError()
                self._error.__cause__ = exc
            ready.set()
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            ready.set()
            finished.set()

    def _abort_start(self, path: Path) -> None:
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join(self._stop_timeout_seconds)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._clear_active_state()
        if thread is None or not thread.is_alive():
            self._cleanup_owned_path(path)

    def _clear_active_state(self) -> None:
        self._active_path = None
        self._thread = None
        self._stop_event = None
        self._ready_event = None
        self._finished_event = None

    def _cleanup_owned_path(self, path: Path) -> None:
        resolved = path.resolve()
        with self._lock:
            if resolved not in self._owned_paths:
                raise DesktopStateError
        try:
            resolved.unlink(missing_ok=True)
        except OSError as exc:
            raise DesktopRecordingError from exc
        with self._lock:
            self._owned_paths.discard(resolved)
