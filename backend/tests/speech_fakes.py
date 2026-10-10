from __future__ import annotations
import asyncio
import io
import wave

from backend.tests.test_memory_rag import FakeLLM
from kulai_memory.application.speech import SpeechLanguage, SpeechPlan, SpeechSegment, SynthesizedSpeech

PL = 'Zielony smok mieszka na Wenus.'
EN = 'The green dragon lives on Venus.'
MIXED = 'Smok mieszka na Venus. The dragon is happy. Potem wraca do domu.'
MIXED_PARTS = [('Smok mieszka na ', 'pl'), ('Venus. The dragon is happy. ', 'en'), ('Potem wraca do domu.', 'pl')]


def wav_bytes(*, rate=22050, channels=1, width=2):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(bytes(channels * width * (rate // 50)))
    return stream.getvalue()


def speech_plan(answer=MIXED, parts=None):
    if parts is None:
        parts = MIXED_PARTS if answer == MIXED else [(answer, 'pl')]
    return SpeechPlan(answer, tuple(SpeechSegment(text, SpeechLanguage(language)) for text, language in parts))


class PlannerLLM(FakeLLM):
    def __init__(self, parts):
        super().__init__()
        self.output = {'segments': [{'text': t, 'language': lang} for t, lang in parts]}


class FakeSpeechProvider:
    def __init__(self):
        self.calls, self.closed = [], 0
        self.error_at = None
        self.prepared = 0

    async def prepare(self):
        self.prepared += 1

    async def synthesize(self, segment):
        self.calls.append(segment)
        if len(self.calls) == self.error_at:
            raise RuntimeError('PRIVATE_ANSWER_PATH_PAYLOAD')
        return SynthesizedSpeech(wav_bytes(), segment.language, 'voice-' + segment.language.value)

    async def aclose(self):
        self.closed += 1


class FakePlayback:
    def __init__(self):
        self.paths, self.contents = [], []
        self.stops = 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.error = False
        self.reading = False

    async def play(self, path):
        self.reading = True
        try:
            self.paths.append(path)
            self.contents.append(path.read_bytes())
            self.entered.set()
            await self.release.wait()
            if self.error:
                raise RuntimeError('PRIVATE_PLAYBACK_PATH')
        finally:
            self.reading = False

    async def stop(self):
        self.stops += 1
        assert not self.reading
