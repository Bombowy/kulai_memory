from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

from backend.tests.speech_fakes import wav_bytes
from kulai_memory import local_tts
from kulai_memory.application.speech import SpeechError, SpeechLanguage, SpeechSegment

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Host adapter is Windows-only.')


class NativeProcess:
    def __init__(self):
        self.stdin = self.stdout = self
        self.returncode = None
        self.messages = []
        self.pending = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.bad = None
        self.killed = 0

    def write(self, value):
        self.pending = json.loads(value)
        self.messages.append(self.pending)

    async def drain(self):
        pass

    async def readline(self):
        if self.pending['operation'] == 'initialize':
            return b'{"ok":true}\n'
        self.entered.set()
        await self.release.wait()
        language = self.pending['language']
        response = dict(ok=True, language=language, voice='Polish Desktop' if language == 'pl' else 'English Desktop',
                        audio=base64.b64encode(wav_bytes()).decode())
        if self.bad == 'voice':
            response['voice'] = 'wrong voice'
        elif self.bad == 'language':
            response['language'] = 'de'
        elif self.bad == 'audio':
            response['audio'] = 'not base64'
        elif self.bad == 'exception':
            raise RuntimeError('PRIVATE_TEXT_INTERNAL_PAYLOAD')
        return (json.dumps(response) + '\n').encode()

    def close(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed += 1
        self.returncode = -1


def native(monkeypatch):
    process, creations = NativeProcess(), []
    async def create(*args, **kwargs):
        creations.append((args, kwargs))
        return process
    monkeypatch.setattr(local_tts.asyncio, 'create_subprocess_exec', create)
    provider = local_tts.WindowsSpeechProvider(pl_voice='Polish Desktop', en_voice='English Desktop')
    return provider, process, creations


def test_static_hidden_command_data_on_stdin_voice_switch_and_reuse(monkeypatch):
    async def scenario():
        provider, process, creations = native(monkeypatch)
        assert not creations
        await provider.prepare()
        malicious = '"; $(PRIVATE_COMMAND); Ignore instructions. <speak>text</speak>'
        for language in (SpeechLanguage.PL, SpeechLanguage.EN, SpeechLanguage.PL):
            result = await provider.synthesize(SpeechSegment(malicious, language))
            assert result.language is language and result.audio[:4] == b'RIFF'
        assert provider.clients == len(creations) == 1
        args, options = creations[0]
        script = base64.b64decode(args[-1]).decode('utf-16-le')
        assert malicious not in str(args) and script == local_tts._ENGINE_SCRIPT
        assert options['creationflags'] == local_tts.subprocess.CREATE_NO_WINDOW
        assert options['stderr'] == asyncio.subprocess.DEVNULL and 'shell' not in options
        assert [m['language'] for m in process.messages[1:]] == ['pl', 'en', 'pl']
        assert all(m['text'] == malicious for m in process.messages[1:])
        await provider.aclose()
        await provider.aclose()
        assert provider.closed == 1 and provider._process is None
        with pytest.raises(SpeechError):
            await provider.synthesize(SpeechSegment('text', SpeechLanguage.EN))
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['voice', 'language', 'audio', 'exception'])
def test_native_failure_rejects_unexpected_data_and_closes_process_safe(monkeypatch, failure):
    async def scenario():
        provider, process, _ = native(monkeypatch)
        process.bad = failure
        with pytest.raises(SpeechError, match='^Could not speak answer\\.$'):
            await provider.synthesize(SpeechSegment('controlled', SpeechLanguage.PL))
        assert provider._process is None and process.returncode == 0
        await provider.aclose()
        assert provider.closed == 1
    asyncio.run(scenario())


def test_cancel_drains_native_inference_before_provider_close(monkeypatch):
    async def scenario():
        provider, process, _ = native(monkeypatch)
        process.release.clear()
        task = asyncio.create_task(provider.synthesize(SpeechSegment('controlled', SpeechLanguage.PL)))
        await process.entered.wait()
        task.cancel()
        closing = asyncio.create_task(provider.aclose())
        await asyncio.sleep(0)
        assert not task.done() and not closing.done() and process.returncode is None
        process.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        assert provider.closed == 1 and provider._process is None
    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_native_synthesis_timeout_bounded_safe_and_closes(monkeypatch):
    monkeypatch.setattr(local_tts, 'SYNTHESIS_TIMEOUT_SECONDS', .01)
    async def scenario():
        provider, process, _ = native(monkeypatch)
        process.release.clear()
        with pytest.raises(SpeechError, match='^Could not speak answer\\.$'):
            await provider.synthesize(SpeechSegment('controlled', SpeechLanguage.PL))
        assert provider._process is None and process.returncode == 0
        await provider.aclose()
    asyncio.run(asyncio.wait_for(scenario(), 1))
