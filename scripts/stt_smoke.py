"""Run the real host speech-to-text path without touching persistence."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from kulai_transcription import (  # noqa: E402
    AudioPathInput,
    TranscriptionError,
    TranscriptionMode,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionTimestampMode,
)

from kulai_memory.application import (  # noqa: E402
    TranscriptFinalEvent,
    TranscriptionService,
    VoiceSession,
    VoiceSessionEvent,
)
from kulai_memory.settings import get_settings  # noqa: E402
from kulai_memory.whisper_provider import (  # noqa: E402
    create_whisper_transcription_provider,
)


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[VoiceSessionEvent] = []

    async def emit(self, event: VoiceSessionEvent) -> None:
        self.events.append(event)


def _write_silence(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(bytes(16_000 * 2))


def _cache_key(model: str) -> str | None:
    if Path(model).exists():
        return None
    return model if "/" in model else f"Systran/faster-whisper-{model}"


def _is_cached(model: str) -> bool | None:
    repo_id = _cache_key(model)
    if repo_id is None:
        return None
    try:
        huggingface_hub = importlib.import_module("huggingface_hub")
        try_to_load_from_cache = getattr(
            huggingface_hub,
            "try_to_load_from_cache",
        )
        cached = try_to_load_from_cache(repo_id=repo_id, filename="model.bin")
    except Exception:
        return None
    return isinstance(cached, str)


async def _run(audio_path: Path, *, language: str | None, show_text: bool) -> None:
    settings = get_settings()
    cache_before = _is_cached(settings.kulai_whisper_model)
    provider = create_whisper_transcription_provider(settings=settings)
    service = TranscriptionService(provider=provider)
    sink = _CollectingSink()
    session = VoiceSession(event_sink=sink, transcription_service=service)
    request = TranscriptionRequest(
        audio=AudioPathInput(path=audio_path),
        language_hint=language,
        mode=TranscriptionMode.TRANSCRIBE,
        timestamp_mode=TranscriptionTimestampMode.NONE,
    )

    await session.start()
    started = time.perf_counter()
    first_result = await session.transcribe(request=request)
    first_seconds = time.perf_counter() - started
    loaded_model = getattr(provider, "_model", None)

    started = time.perf_counter()
    second_result = await session.transcribe(request=request)
    second_seconds = time.perf_counter() - started
    reused_model = loaded_model is not None and getattr(provider, "_model", None) is loaded_model
    await session.close()

    finals = [event for event in sink.events if isinstance(event, TranscriptFinalEvent)]
    if len(finals) != 2 or [event.sequence for event in sink.events] != [1, 2, 3]:
        raise RuntimeError("Host event contract validation failed.")
    if not isinstance(first_result, TranscriptionResult) or not isinstance(
        second_result, TranscriptionResult
    ):
        raise RuntimeError("Host result contract validation failed.")

    cache_after = _is_cached(settings.kulai_whisper_model)
    if cache_before is True:
        cache_status = "already-cached"
    elif cache_before is False and cache_after is True:
        cache_status = "downloaded"
    else:
        cache_status = "unknown"

    language_code = first_result.language.code if first_result.language else None
    print("STT smoke PASS")
    print(f"model_id={first_result.model_id or 'unknown'}")
    print(f"device={settings.kulai_whisper_device}")
    print(f"compute_type={settings.kulai_whisper_compute_type}")
    print(f"cache_status={cache_status}")
    print(f"first_inference_seconds={first_seconds:.3f}")
    print(f"second_inference_seconds={second_seconds:.3f}")
    print(f"model_reused={str(reused_model).lower()}")
    print(f"result_type={type(first_result).__name__}")
    print(f"language={language_code or 'unknown'}")
    print(
        "duration_seconds="
        f"{first_result.duration_seconds if first_result.duration_seconds is not None else 'unknown'}"
    )
    print(f"segment_count={len(first_result.segments)}")
    print(f"transcript_character_count={len(first_result.text)}")
    if show_text:
        print("transcript:")
        print(first_result.text)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, help="Optional local speech sample.")
    parser.add_argument("--language", help="Optional language hint, for example pl.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.audio is not None:
            asyncio.run(_run(args.audio, language=args.language, show_text=True))
        else:
            with tempfile.TemporaryDirectory(prefix="kulai-stt-") as temp_dir:
                audio_path = Path(temp_dir) / "silence.wav"
                _write_silence(audio_path)
                asyncio.run(_run(audio_path, language=args.language, show_text=False))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"STT smoke FAIL ({type(exc).__name__})", file=sys.stderr)
        cause = exc.__cause__
        if isinstance(cause, TranscriptionError):
            print(f"provider_error_type={type(cause).__name__}", file=sys.stderr)
            for key in ("stage", "error_type", "device", "input_kind"):
                value = cause.internal_details.get(key)
                if isinstance(value, str):
                    print(f"{key}={value}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
