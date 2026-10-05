from __future__ import annotations

import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from kulai_memory.desktop.models import DesktopStateError, RecordingArtifact
from kulai_memory.desktop.recorder import (
    CHANNELS,
    DTYPE,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    MicrophoneRecorder,
)


class FakeStream:
    def __init__(self, backend: FakeSoundDevice, kwargs: dict[str, object]) -> None:
        self.backend = backend
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def read(self, frames: int) -> tuple[bytes, bool]:
        self.backend.read_count += 1
        if self.backend.read_count >= 2:
            self.backend.read_complete.set()
        return bytes(frames * SAMPLE_WIDTH), False


class PairLikeDefault:
    def __init__(self, input_id: int, output_id: int) -> None:
        self._values = (input_id, output_id)

    def __getitem__(self, index: int) -> int:
        return self._values[index]

    def __repr__(self) -> str:
        return f"[{self._values[0]}, {self._values[1]}]"


class FakeSoundDevice:
    def __init__(
        self,
        *,
        default_device: object = (1, 4),
        devices: tuple[dict[str, object], ...] | None = None,
    ) -> None:
        self.default = SimpleNamespace(device=default_device)
        self.devices = devices or (
            {"name": "Speakers", "max_input_channels": 0, "hostapi": 0},
            {"name": "Default mic", "max_input_channels": 2, "hostapi": 0},
            {"name": "USB mic", "max_input_channels": 1, "hostapi": 1},
        )
        self.checked: list[dict[str, object]] = []
        self.streams: list[FakeStream] = []
        self.read_count = 0
        self.read_complete = threading.Event()

    def query_devices(self) -> tuple[dict[str, object], ...]:
        return self.devices

    def query_hostapis(self) -> tuple[dict[str, object], ...]:
        return ({"name": "WASAPI"}, {"name": "MME"})

    def check_input_settings(self, **kwargs: object) -> None:
        self.checked.append(kwargs)

    def RawInputStream(self, **kwargs: object) -> FakeStream:
        stream = FakeStream(self, kwargs)
        self.streams.append(stream)
        return stream


def _recorder(tmp_path: Path) -> tuple[MicrophoneRecorder, FakeSoundDevice]:
    backend = FakeSoundDevice()
    recorder = MicrophoneRecorder(
        sounddevice_backend=backend,
        temp_directory=tmp_path,
        max_duration_seconds=0.05,
        block_frames=400,
    )
    return recorder, backend


def test_device_enumeration_maps_inputs_and_default(tmp_path: Path) -> None:
    recorder, _ = _recorder(tmp_path)

    devices = recorder.list_devices()

    assert [(item.device_id, item.name, item.host_api) for item in devices] == [
        (1, "Default mic", "WASAPI"),
        (2, "USB mic", "MME"),
    ]
    assert [item.is_default for item in devices] == [True, False]
    assert "default" in devices[0].display_name


def test_pair_like_default_uses_input_side_without_private_type_dependency(
    tmp_path: Path,
) -> None:
    raw_devices = (
        {"name": "Output", "max_input_channels": 0, "hostapi": 0},
        {"name": "Input default", "max_input_channels": 1, "hostapi": 0},
        {"name": "Other input", "max_input_channels": 1, "hostapi": 1},
        {"name": "Output-selected duplex", "max_input_channels": 1, "hostapi": 1},
    )
    pair = PairLikeDefault(1, 3)
    assert repr(pair) == "[1, 3]"
    assert not isinstance(pair, (list, tuple))
    backend = FakeSoundDevice(default_device=pair, devices=raw_devices)
    recorder = MicrophoneRecorder(
        sounddevice_backend=backend,
        temp_directory=tmp_path,
    )

    devices = recorder.list_devices()

    assert [device.device_id for device in devices if device.is_default] == [1]
    assert next(device for device in devices if device.device_id == 3).is_default is False


def test_scalar_default_is_supported(tmp_path: Path) -> None:
    backend = FakeSoundDevice(default_device=2)
    recorder = MicrophoneRecorder(
        sounddevice_backend=backend,
        temp_directory=tmp_path,
    )

    devices = recorder.list_devices()

    assert [device.device_id for device in devices if device.is_default] == [2]


@pytest.mark.parametrize("default_device", [object(), -1])
def test_unavailable_default_does_not_break_enumeration(
    tmp_path: Path,
    default_device: object,
) -> None:
    backend = FakeSoundDevice(default_device=default_device)
    recorder = MicrophoneRecorder(
        sounddevice_backend=backend,
        temp_directory=tmp_path,
    )

    devices = recorder.list_devices()

    assert [device.device_id for device in devices] == [1, 2]
    assert not any(device.is_default for device in devices)


def test_start_stop_writes_bounded_pcm16_mono_wav_and_cleanup(tmp_path: Path) -> None:
    recorder, backend = _recorder(tmp_path)

    recorder.start(device_id=2)
    assert backend.read_complete.wait(1.0)
    artifact = recorder.stop()

    assert artifact.path.parent == tmp_path.resolve()
    assert artifact.limit_reached is True
    assert artifact.duration_seconds == pytest.approx(0.05)
    assert backend.checked == [
        {
            "device": 2,
            "channels": CHANNELS,
            "dtype": DTYPE,
            "samplerate": SAMPLE_RATE,
        }
    ]
    assert backend.streams[0].kwargs["device"] == 2
    with wave.open(str(artifact.path), "rb") as recorded:
        assert recorded.getframerate() == SAMPLE_RATE
        assert recorded.getnchannels() == CHANNELS
        assert recorded.getsampwidth() == SAMPLE_WIDTH
        assert recorded.getnframes() == round(0.05 * SAMPLE_RATE)

    recorder.cleanup(artifact)

    assert not artifact.path.exists()
    with pytest.raises(DesktopStateError):
        recorder.cleanup(artifact)


def test_invalid_lifecycle_and_foreign_cleanup_are_rejected(tmp_path: Path) -> None:
    recorder, _ = _recorder(tmp_path)

    with pytest.raises(DesktopStateError):
        recorder.stop()

    recorder.start(device_id=1)
    with pytest.raises(DesktopStateError):
        recorder.start(device_id=1)
    artifact = recorder.stop()

    foreign = tmp_path / "foreign.wav"
    foreign.write_bytes(b"do not remove")
    with pytest.raises(DesktopStateError):
        recorder.cleanup(
            RecordingArtifact(
                path=foreign,
                duration_seconds=0.0,
                limit_reached=False,
            )
        )
    assert foreign.read_bytes() == b"do not remove"
    recorder.cleanup(artifact)


def test_shutdown_stops_active_recording_and_removes_owned_file(tmp_path: Path) -> None:
    recorder, _ = _recorder(tmp_path)
    recorder.start(device_id=1)

    recorder.shutdown()
    recorder.shutdown()

    assert list(tmp_path.glob("kulai-memory-recording-*.wav")) == []
