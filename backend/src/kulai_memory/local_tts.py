"""Local Windows System.Speech adapter; one lazy process and installed PL/EN voices.

Only static code enters PowerShell's command argument. Answer text is JSON data on
stdin, never shell code or SSML. No model download, network client, or audio device.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

from .application.speech import MAX_SPEECH_CHARS, SpeechError, SpeechLanguage, SpeechSegment, SynthesizedSpeech

SYNTHESIS_TIMEOUT_SECONDS = 90.0
MAX_AUDIO_BYTES = 16 * 1024 * 1024

# System.Speech is already installed Windows tooling. Keep the native object alive
# across segments, switch explicit installed voices, and return complete WAV bytes.
_ENGINE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$voices = @{}
try {
    while ($null -ne ($line = [Console]::ReadLine())) {
        try {
            $request = ConvertFrom-Json -InputObject $line
            if ($request.operation -eq 'close') { break }
            if ($request.operation -eq 'initialize') {
                $voices = @{}
                foreach ($language in @('pl', 'en')) {
                    $name = $request.voices.$language
                    $installed = @($synth.GetInstalledVoices() | Where-Object {
                        $_.Enabled -and $_.VoiceInfo.Name -ceq $name -and
                        $_.VoiceInfo.Culture.TwoLetterISOLanguageName -ceq $language -and
                        $_.VoiceInfo.AdditionalInfo['Vendor'] -ceq 'Microsoft' -and
                        $_.VoiceInfo.Name.EndsWith(' Desktop')
                    })
                    if ($installed.Count -ne 1) { throw 'unavailable' }
                    $voices[$language] = $installed[0].VoiceInfo.Name
                }
                [Console]::WriteLine('{"ok":true}')
            } elseif ($request.operation -eq 'synthesize') {
                if (!$voices.ContainsKey($request.language)) { throw 'unsupported' }
                $stream = [System.IO.MemoryStream]::new()
                try {
                    $synth.SelectVoice($voices[$request.language])
                    $synth.SetOutputToWaveStream($stream)
                    $synth.Speak([string]$request.text)
                    $synth.SetOutputToNull()
                    $response = @{ok=$true; voice=$synth.Voice.Name;
                        language=$request.language; audio=[Convert]::ToBase64String($stream.ToArray())}
                    [Console]::WriteLine((ConvertTo-Json -InputObject $response -Compress))
                } finally { $synth.SetOutputToNull(); $stream.Dispose() }
            } else { throw 'unsupported' }
        } catch { [Console]::WriteLine('{"ok":false}') }
    }
} finally { $synth.Dispose() }
'''


async def drain_audio_work(task: asyncio.Task):
    """Do not discard resources while a native/thread-backed operation reads them."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class WindowsSpeechProvider:
    provider_id = "windows-system-speech"

    def __init__(self, *, pl_voice: str, en_voice: str):
        if os.name != "nt" or not pl_voice.strip() or not en_voice.strip() or pl_voice == en_voice:
            raise SpeechError()
        self.voices = {SpeechLanguage.PL: pl_voice, SpeechLanguage.EN: en_voice}
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.clients = self.closed = 0
        self.requests = {SpeechLanguage.PL: 0, SpeechLanguage.EN: 0}

    async def _exchange(self, request: dict) -> dict:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise SpeechError()
        process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()
        line = await process.stdout.readline()
        response = json.loads(line)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise SpeechError()
        return response

    async def _initialize(self):
        if self._process is not None:
            return
        # Resolve Windows' own PowerShell, without consulting a user command/PATH.
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not executable.is_file():
            raise SpeechError()
        encoded = base64.b64encode(_ENGINE_SCRIPT.encode("utf-16-le")).decode("ascii")
        self._process = await asyncio.create_subprocess_exec(str(executable), "-NoLogo", "-NoProfile",
            "-NonInteractive", "-EncodedCommand", encoded, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_AUDIO_BYTES * 2, creationflags=subprocess.CREATE_NO_WINDOW)
        self.clients += 1
        await self._exchange({"operation": "initialize", "voices": {k.value: v for k, v in self.voices.items()}})

    async def synthesize(self, segment: SpeechSegment) -> SynthesizedSpeech:
        if self._closed or not isinstance(segment, SpeechSegment) or len(segment.text) > MAX_SPEECH_CHARS:
            raise SpeechError()
        async with self._lock:
            async def work():
                try:
                    async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS):
                        await self._initialize()
                        self.requests[segment.language] += 1
                        response = await self._exchange({"operation": "synthesize",
                            "language": segment.language.value, "text": segment.text})
                        if response.get("language") != segment.language.value or response.get("voice") != self.voices[segment.language]:
                            raise SpeechError()
                        audio = base64.b64decode(response["audio"], validate=True)
                        if not audio or len(audio) > MAX_AUDIO_BYTES:
                            raise SpeechError()
                        return SynthesizedSpeech(audio, segment.language, response["voice"])
                except Exception:
                    await self._stop_process()
                    raise SpeechError() from None
            return await drain_audio_work(asyncio.create_task(work()))

    async def prepare(self) -> None:
        """Check both installed voices without performing synthesis or loading weights."""
        if self._closed:
            raise SpeechError()
        async with self._lock:
            async def work():
                try:
                    async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS):
                        await self._initialize()
                except Exception:
                    await self._stop_process()
                    raise SpeechError() from None
            await drain_audio_work(asyncio.create_task(work()))

    async def _stop_process(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.returncode is None:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        finally:
            if process.stdin is not None:
                process.stdin.close()

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            await drain_audio_work(asyncio.create_task(self._stop_process()))
            self.closed += 1
