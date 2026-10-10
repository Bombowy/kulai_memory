"""Synthetic PL/EN coverage and a controlled native audio-device boundary."""
from backend.tests.speech_fakes import PL, EN

# Full English clauses must be English, even within one sentence. Proper names
# can belong to the surrounding phrase. Spaces can belong to either neighbour.
PL_EN_CASES = (
    (PL, ('pl',), ()),
    (EN, ('en',), (EN,)),
    ('To jest test. This is a test.', ('pl', 'en'), ('This is a test.',)),
    ('This is a test. To jest test.', ('en', 'pl'), ('This is a test.',)),
    ('Smok mieszka na Venus. The dragon is happy. Potem śpi.', ('pl', 'en', 'pl'), ('The dragon is happy.',)),
    ('The dragon lives on Venus. Następnie wraca do domu.', ('en', 'pl'), ('The dragon lives on Venus.',)),
    ('Używam FastAPI and PostgreSQL in this project.', ('pl', 'en'), ('and PostgreSQL in this project.',)),
    ('Model qwen3.5:9b działa lokalnie. The API is ready.', ('pl', 'en'), ('The API is ready.',)),
    ('To pole nazywa się User ID and it is required.', ('pl', 'en'), ('User ID and it is required.',)),
    ('English sentence first. Potem polskie zdanie. Another English sentence.',
     ('en', 'pl', 'en'), ('English sentence first.', 'Another English sentence.')),
    ('To jest odpowiedź po polsku. This is the English part. I znowu po polsku.',
     ('pl', 'en', 'pl'), ('This is the English part.',)),
    ('This starts in English. Potem przechodzimy na polski. English again.',
     ('en', 'pl', 'en'), ('This starts in English.', 'English again.')),
    ('Używam FastAPI. This API is running locally.', ('pl', 'en'), ('This API is running locally.',)),
    ('Nice to meet you. Miło cię poznać.', ('en', 'pl'), ('Nice to meet you.',)),
    ('To jest część po polsku. This is the English part. I znowu po polsku.',
     ('pl', 'en', 'pl'), ('This is the English part.',)),
)


def assert_language_coverage(plan, answer, languages, english_phrases):
    assert plan.original_answer == answer
    assert ''.join(segment.text for segment in plan.segments) == answer
    assert tuple(segment.language.value for segment in plan.segments) == languages
    labels = [segment.language.value for segment in plan.segments for _ in segment.text]
    for phrase in english_phrases:
        start = answer.index(phrase)
        assert labels[start:start + len(phrase)] == ['en'] * len(phrase)


class CompleteAudioDevice:
    """Consume every PCM callback, without playing user audio or touching hardware."""
    class CallbackStop(Exception):
        pass

    def __init__(self):
        self.streams = []
        self.active = 0
        self.maximum_active = 0

    def RawOutputStream(self, **kwargs):
        owner = self
        class Stream:
            aborted = closed = 0
            def __init__(self):
                self.output = bytearray()
                self.parameters = kwargs
            def start(self):
                owner.active += 1
                owner.maximum_active = max(owner.maximum_active, owner.active)
                while True:
                    chunk = bytearray(1024 * kwargs['channels'] * 2)
                    try:
                        kwargs['callback'](chunk, 1024, None, None)
                    except owner.CallbackStop:
                        self.output.extend(chunk)
                        kwargs['finished_callback']()
                        break
                    self.output.extend(chunk)
            def abort(self):
                self.aborted += 1
            def close(self):
                self.closed += 1
                owner.active -= 1
        stream = Stream()
        self.streams.append(stream)
        return stream
