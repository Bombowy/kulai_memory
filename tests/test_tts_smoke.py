from __future__ import annotations
import asyncio
from types import SimpleNamespace

import pytest
from scripts import tts_smoke as cli
from backend.tests.speech_fakes import FakeSpeechProvider, FakePlayback, PlannerLLM, MIXED, MIXED_PARTS
from kulai_memory.speech_runtime import SpeechRuntime


@pytest.fixture
def fixture(monkeypatch):
    provider, player, llm = FakeSpeechProvider(), FakePlayback(), PlannerLLM(MIXED_PARTS)
    runtime = SpeechRuntime(provider=provider, playback=player)
    async def close_llm():
        await llm.__aexit__(None, None, None)
    llm.aclose = close_llm
    state = SimpleNamespace(provider=provider, player=player, llm=llm, runtime=runtime, created=0)
    monkeypatch.setattr(cli, 'get_settings', lambda: object())
    def create(**kwargs):
        state.created += 1
        return runtime
    monkeypatch.setattr(cli, 'create_speech_runtime', create)
    monkeypatch.setattr(cli, 'create_llm_provider', lambda **kwargs: llm)
    return state


@pytest.mark.parametrize('no_play', [False, True])
def test_diagnostic_uses_existing_runtime_only_reports_safe_metadata(fixture, capsys, no_play):
    assert cli.main(['--text', MIXED] + (['--no-play'] if no_play else [])) == 0
    out = capsys.readouterr()
    assert not out.err and MIXED not in out.out
    assert out.out.splitlines()[0] == 'segment_count=3'
    assert ['language=pl' in out.out, 'language=en' in out.out] == [True, True]
    assert len(fixture.provider.calls) == 3 and len(fixture.llm.calls) == 1
    assert fixture.provider.closed == fixture.llm.closed == 1 and not fixture.runtime.artifacts
    assert len(fixture.player.paths) == (0 if no_play else 3)
    assert all(not path.exists() for path in fixture.player.paths)


@pytest.mark.parametrize('text', ['', ' \t', 'x' * 6001])
def test_invalid_text_before_any_resource(fixture, capsys, text):
    assert cli.main(['--text', text]) == 1
    assert fixture.created == 0 and not fixture.llm.calls and not fixture.provider.calls
    assert capsys.readouterr().err.strip() == 'Could not speak answer.'


@pytest.mark.parametrize('failure', ['unavailable', 'planner', 'invalid', 'synthesis', 'playback'])
def test_safe_failure_cleanup_no_pl_fallback(fixture, monkeypatch, capsys, failure):
    if failure == 'unavailable':
        monkeypatch.setattr(cli, 'create_speech_runtime', lambda **kwargs: None)
    elif failure == 'planner':
        fixture.llm.error = RuntimeError('PRIVATE_ANSWER_PATH')
    elif failure == 'invalid':
        fixture.llm.output['segments'][0]['text'] = 'rewritten'
    elif failure == 'synthesis':
        fixture.provider.error_at = 2
    else:
        fixture.player.error = True
    assert cli.main(['--text', MIXED]) == 1
    out = capsys.readouterr()
    assert out.err.strip() == 'Could not speak answer.' and 'PRIVATE' not in out.out + out.err
    assert MIXED not in out.out + out.err and not fixture.runtime.artifacts
    if failure in {'planner', 'invalid'}:
        assert not fixture.provider.calls
    if failure != 'unavailable':
        assert fixture.provider.closed == 1


def test_cancellation_propagates_and_closes_owned_resources(fixture):
    fixture.llm.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli.run(SimpleNamespace(text=MIXED, no_play=False)))
    assert fixture.provider.closed == fixture.llm.closed == 1 and not fixture.runtime.artifacts


def test_parser_does_not_echo_private_text(fixture, capsys):
    with pytest.raises(SystemExit):
        cli.main(['--text', 'PRIVATE', '--unknown', 'PRIVATE'])
    assert 'PRIVATE' not in capsys.readouterr().err and fixture.created == 0
