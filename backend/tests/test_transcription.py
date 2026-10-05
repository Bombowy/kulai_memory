from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import sqlalchemy.ext.asyncio
from kulai_transcription import (
    AudioBytesInput,
    AudioInputKind,
    AudioPathInput,
    TranscriptionCapabilities,
    TranscriptionLanguage,
    TranscriptionLanguageSource,
    TranscriptionMode,
    TranscriptionProviderError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionTimestampMode,
)

from kulai_memory.application import (
    MemoryService,
    TranscriptFinalEvent,
    TranscriptionService,
    VoiceSession,
    VoiceSessionEvent,
    VoiceSessionEventType,
    VoiceSessionState,
    VoiceSessionStateError,
    VoiceSessionTranscriptionError,
    event_to_jsonable,
)


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[VoiceSessionEvent] = []

    async def emit(self, event: VoiceSessionEvent) -> None:
        self.events.append(event)


class RecordingProvider:
    provider_id = "recording-fake"
    capabilities = TranscriptionCapabilities(
        input_kinds=frozenset({AudioInputKind.BYTES, AudioInputKind.PATH}),
        modes=frozenset({TranscriptionMode.TRANSCRIBE}),
        supports_language_hint=True,
        supports_language_detection=True,
        supports_segments=True,
        supports_segment_timestamps=True,
    )

    def __init__(self, *, texts: tuple[str, ...] = ("recognized",)) -> None:
        self.requests: list[TranscriptionRequest] = []
        self._texts = texts

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        text = self._texts[min(len(self.requests) - 1, len(self._texts) - 1)]
        segments = (
            (
                TranscriptionSegment(
                    text=text,
                    start_seconds=0.0,
                    end_seconds=1.0,
                    segment_id="fake-segment",
                ),
            )
            if text and request.timestamp_mode is TranscriptionTimestampMode.SEGMENT
            else ()
        )
        return TranscriptionResult(
            text=text,
            provider_id=self.provider_id,
            mode=request.mode,
            model_id="fake-model",
            language=TranscriptionLanguage(
                code=request.language_hint or "pl",
                source=(
                    TranscriptionLanguageSource.REQUESTED
                    if request.language_hint
                    else TranscriptionLanguageSource.DETECTED
                ),
            ),
            segments=segments,
            duration_seconds=1.0,
        )


def _bytes_request(
        *,
        language_hint: str | None = None,
        timestamp_mode: TranscriptionTimestampMode = TranscriptionTimestampMode.NONE,
) -> TranscriptionRequest:
    return TranscriptionRequest(
        audio=AudioBytesInput(data=b"RIFF-safe-test-audio", media_type="audio/wav"),
        language_hint=language_hint,
        mode=TranscriptionMode.TRANSCRIBE,
        timestamp_mode=timestamp_mode,
    )


def test_application_service_preserves_bytes_path_language_and_timestamp_mode() -> None:
    async def scenario() -> None:
        provider = RecordingProvider()
        service = TranscriptionService(provider=provider)
        bytes_request = _bytes_request(
            language_hint="pl",
            timestamp_mode=TranscriptionTimestampMode.SEGMENT,
        )
        path_request = TranscriptionRequest(
            audio=AudioPathInput(path=Path("voice.wav")),
            language_hint="en",
            mode=TranscriptionMode.TRANSCRIBE,
        )

        bytes_result = await service.transcribe(request=bytes_request)
        path_result = await service.transcribe(request=path_request)

        assert provider.requests == [bytes_request, path_request]
        assert isinstance(provider.requests[0].audio, AudioBytesInput)
        assert isinstance(provider.requests[1].audio, AudioPathInput)
        assert provider.requests[0].language_hint == "pl"
        assert provider.requests[0].timestamp_mode is TranscriptionTimestampMode.SEGMENT
        assert bytes_result.text == "recognized"
        assert path_result.provider_id == provider.provider_id

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["recognized", ""])
def test_session_returns_result_and_emits_one_final_including_silence(text: str) -> None:
    async def scenario() -> None:
        sink = InMemoryEventSink()
        provider = RecordingProvider(texts=(text,))
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=provider),
        )
        await session.start()

        result = await session.transcribe(request=_bytes_request())

        assert result.text == text
        finals = [event for event in sink.events if isinstance(event, TranscriptFinalEvent)]
        assert len(finals) == 1
        assert finals[0].payload.text == text
        assert [event.sequence for event in sink.events] == [1, 2]
        assert [event.type for event in sink.events] == [
            VoiceSessionEventType.SESSION_READY,
            VoiceSessionEventType.TRANSCRIPT_FINAL,
        ]

    asyncio.run(scenario())


def test_two_concurrent_requests_are_serialized_deterministically() -> None:
    class CoordinatedProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__(texts=("first", "second"))
            self.first_entered = asyncio.Event()
            self.release_first = asyncio.Event()
            self.active_calls = 0
            self.max_active_calls = 0

        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            if not self.requests:
                self.first_entered.set()
                await self.release_first.wait()
            try:
                return await super().transcribe(request)
            finally:
                self.active_calls -= 1

    async def scenario() -> None:
        sink = InMemoryEventSink()
        provider = CoordinatedProvider()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=provider),
        )
        await session.start()

        first = asyncio.create_task(session.transcribe(request=_bytes_request()))
        await provider.first_entered.wait()
        second = asyncio.create_task(session.transcribe(request=_bytes_request()))
        await asyncio.sleep(0)
        assert len(provider.requests) == 0

        provider.release_first.set()
        results = await asyncio.gather(first, second)

        assert [result.text for result in results] == ["first", "second"]
        assert provider.max_active_calls == 1
        assert [event.sequence for event in sink.events] == [1, 2, 3]
        assert [event.payload.text for event in sink.events[1:]] == ["first", "second"]

    asyncio.run(scenario())


def test_close_waits_for_inference_and_closes_after_final_event() -> None:
    class BlockingProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            self.entered.set()
            await self.release.wait()
            return await super().transcribe(request)

    async def scenario() -> None:
        sink = InMemoryEventSink()
        provider = BlockingProvider()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=provider),
        )
        await session.start()
        transcription = asyncio.create_task(session.transcribe(request=_bytes_request()))
        await provider.entered.wait()
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)

        assert not closing.done()
        assert session.state is VoiceSessionState.ACTIVE

        provider.release.set()
        result = await transcription
        await closing

        assert result.text == "recognized"
        assert session.state is VoiceSessionState.CLOSED
        assert [event.sequence for event in sink.events] == [1, 2]

    asyncio.run(scenario())


def test_transcribe_rejects_non_active_and_failed_sessions() -> None:
    async def scenario() -> None:
        request = _bytes_request()
        created = VoiceSession(
            event_sink=InMemoryEventSink(),
            transcription_service=TranscriptionService(provider=RecordingProvider()),
        )
        with pytest.raises(VoiceSessionStateError):
            await created.transcribe(request=request)

        await created.start()
        await created.close()
        with pytest.raises(VoiceSessionStateError):
            await created.transcribe(request=request)

        failed = VoiceSession(event_sink=InMemoryEventSink())
        await failed.start()
        with pytest.raises(VoiceSessionTranscriptionError):
            await failed.transcribe(request=request)
        assert failed.state is VoiceSessionState.FAILED
        with pytest.raises(VoiceSessionStateError):
            await failed.transcribe(request=request)

    asyncio.run(scenario())


def test_provider_failure_maps_to_safe_controlled_error_without_private_content() -> None:
    class FailingProvider(RecordingProvider):
        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            del request
            raise TranscriptionProviderError(
                message=(
                    "password=top-secret PRIVATE_TRANSCRIPT_SENTINEL "
                    r"C:\secret\voice.wav"
                )
            )

    async def scenario() -> None:
        sink = InMemoryEventSink()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=FailingProvider()),
        )
        await session.start()

        with pytest.raises(VoiceSessionTranscriptionError) as caught:
            await session.transcribe(request=_bytes_request())

        error = caught.value
        assert isinstance(error.__cause__, TranscriptionProviderError)
        assert session.state is VoiceSessionState.FAILED
        public_event = error.to_event(session_id=session.session_id, sequence=2)
        public_surface = f"{error!s} {event_to_jsonable(public_event)!s}"
        for sentinel in (
                "password=top-secret",
                "PRIVATE_TRANSCRIPT_SENTINEL",
                r"C:\secret\voice.wav",
        ):
            assert sentinel not in public_surface
        assert [event.sequence for event in sink.events] == [1]

    asyncio.run(scenario())


def test_cancellation_propagates_leaves_active_and_emits_no_final() -> None:
    class CancellableProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.never = asyncio.Event()

        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            self.entered.set()
            await self.never.wait()
            return await super().transcribe(request)

    async def scenario() -> None:
        sink = InMemoryEventSink()
        provider = CancellableProvider()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=provider),
        )
        await session.start()
        task = asyncio.create_task(session.transcribe(request=_bytes_request()))
        await provider.entered.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.state is VoiceSessionState.ACTIVE
        assert [event.sequence for event in sink.events] == [1]
        await session.close()

    asyncio.run(scenario())


def test_host_await_does_not_block_the_event_loop() -> None:
    class ThreadOffloadedProvider(RecordingProvider):
        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            await asyncio.to_thread(time.sleep, 0.05)
            return await super().transcribe(request)

    async def scenario() -> None:
        session = VoiceSession(
            event_sink=InMemoryEventSink(),
            transcription_service=TranscriptionService(provider=ThreadOffloadedProvider()),
        )
        await session.start()
        completed_tick = False

        async def tick() -> None:
            nonlocal completed_tick
            await asyncio.sleep(0.01)
            completed_tick = True

        transcription, _ = await asyncio.gather(
            session.transcribe(request=_bytes_request()),
            tick(),
        )

        assert completed_tick
        assert transcription.text == "recognized"

    asyncio.run(scenario())


def test_transcription_flow_does_not_call_memory_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Persistence must not be used by TASK 3A transcription.")

    monkeypatch.setattr(MemoryService, "create_memory", unexpected_call)
    monkeypatch.setattr(
        sqlalchemy.ext.asyncio,
        "create_async_engine",
        unexpected_call,
    )

    async def scenario() -> None:
        sink = InMemoryEventSink()
        session = VoiceSession(
            event_sink=sink,
            transcription_service=TranscriptionService(provider=RecordingProvider()),
        )
        await session.start()
        result = await session.transcribe(request=_bytes_request())
        await session.close()

        assert result.text == "recognized"
        assert [event.sequence for event in sink.events] == [1, 2]

    asyncio.run(scenario())
