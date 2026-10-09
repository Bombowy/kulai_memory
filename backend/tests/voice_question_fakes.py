"""Owned controlled WAV capture; never touches a real microphone/user audio."""
from pathlib import Path
import wave

from kulai_memory.desktop.models import MicrophoneDevice, RecordingArtifact


class OwnedWavRecorder:
    def __init__(self, root: Path):
        self.root = root
        self.active_path = None
        self.owned = set()
        self.cleaned = []
        self.started_devices = []
        self.shutdown_count = 0
        self.index = 0

    def list_devices(self):
        return (MicrophoneDevice(3, 'Controlled WAV', 'Test', True),)

    def start(self, *, device_id):
        assert self.active_path is None
        path = self.root / f'controlled-capture-{self.index}.wav'
        self.index += 1
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(bytes(32000))
        self.active_path = path
        self.owned.add(path)
        self.started_devices.append(device_id)

    def stop(self):
        assert self.active_path is not None
        path, self.active_path = self.active_path, None
        return RecordingArtifact(path, 1.0, False)

    def cleanup(self, artifact):
        assert artifact.path in self.owned and artifact.path != self.active_path
        artifact.path.unlink()
        self.owned.remove(artifact.path)
        self.cleaned.append(artifact.path)

    def shutdown(self):
        self.shutdown_count += 1
        self.active_path = None
        for path in tuple(self.owned):
            self.cleanup(RecordingArtifact(path, 1.0, False))
