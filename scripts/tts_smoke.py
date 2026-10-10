"""Local Polish / English speech diagnostic; synthetic text, no Memory/database."""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / 'backend' / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.application.speech import MAX_SPEECH_CHARS, SpeechError, plan_speech  # noqa: E402
from kulai_memory.llm_provider import create_llm_provider  # noqa: E402
from kulai_memory.local_tts import drain_audio_work  # noqa: E402
from kulai_memory.settings import get_settings  # noqa: E402
from kulai_memory.speech_runtime import SpeechSegmentInfo, create_speech_runtime  # noqa: E402


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        super().error('Provide --text with nonblank text of at most 6000 characters, optionally --no-play.')


class _NoPlayback:
    # SpeechRuntime still synthesizes and validates every WAV and owns cleanup.
    async def play(self, path):
        pass
    async def stop(self):
        pass


def parser():
    result = _SafeParser(description=__doc__)
    result.add_argument('--text', required=True, help='Synthetic test text only, at most 6000 characters.')
    result.add_argument('--no-play', action='store_true', help='Synthesize and validate without audio playback.')
    return result


async def run(args):
    if not isinstance(args.text, str) or not args.text.strip() or len(args.text) > MAX_SPEECH_CHARS:
        raise SpeechError()
    settings = get_settings()
    runtime = create_speech_runtime(settings=settings)
    if runtime is None:
        raise SpeechError()
    llm = None
    try:
        if args.no_play:
            runtime.playback = _NoPlayback()
        await runtime.prepare()
        llm = create_llm_provider(settings=settings)
        plan = await plan_speech(answer=args.text, provider=llm)
        print(f'segment_count={len(plan.segments)}')
        def report(info: SpeechSegmentInfo):
            print(f'segment={info.number} language={info.language.value} chars={info.character_count} voice={info.voice_id}')
        await runtime.speak(plan, on_segment=report)
        return 0
    finally:
        async def close():
            try:
                await runtime.aclose()
            finally:
                if llm is not None:
                    await llm.aclose()
        await drain_audio_work(asyncio.create_task(close()))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return asyncio.run(asyncio.wait_for(run(args), timeout=800))
    except KeyboardInterrupt:
        print('Speech stopped.', file=sys.stderr)
        return 130
    except Exception:
        print('Could not speak answer.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
