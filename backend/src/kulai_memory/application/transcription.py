"""Provider-neutral speech-to-text application boundary."""

from __future__ import annotations

from kulai_transcription import (
    TranscriptionProvider,
    TranscriptionRequest,
    TranscriptionResult,
    transcribe as transcribe_with_provider,
)


class TranscriptionService:
    """Run a transcription request through an injected reusable provider."""

    def __init__(self, *, provider: TranscriptionProvider) -> None:
        self._provider = provider

    async def transcribe(
        self, *, request: TranscriptionRequest
    ) -> TranscriptionResult:
        return await transcribe_with_provider(provider=self._provider, request=request)
