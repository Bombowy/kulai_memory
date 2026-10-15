"""Harness safety/control-flow tests; mocked DB here is not a TLS proof."""
import asyncio
from dataclasses import dataclass
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.tests import security_stream_benchmark as benchmark


def argv(tmp_path):
    return ['--label','verification','--output',str(tmp_path/'result.json'),'--seconds','600']


def test_long_stream_requires_separate_environment_and_flag(tmp_path,monkeypatch):
    monkeypatch.delenv('KULAI_RUN_LONG_TRANSPORT',raising=False)
    with pytest.raises(SystemExit): benchmark.parse_args(argv(tmp_path))
    with pytest.raises(SystemExit): benchmark.parse_args(argv(tmp_path)+['--allow-long-tests'])
    monkeypatch.setenv('KULAI_RUN_LONG_TRANSPORT','1')
    with pytest.raises(SystemExit): benchmark.parse_args(argv(tmp_path))
    args=benchmark.parse_args(argv(tmp_path)+['--allow-long-tests','--sizes','10','100','1000'])
    assert benchmark.timeout_seconds(args)>=3*3*(600+120)


@pytest.mark.parametrize('cancel',[False,True])
def test_failure_and_cancellation_write_redacted_json_after_cleanup(tmp_path,monkeypatch,cancel):
    @dataclass
    class Snapshot: count:int=1
    monkeypatch.setenv('KULAI_RUN_POSTGRES_INTEGRATION','1')
    monkeypatch.setattr(benchmark,'database_config',lambda:SimpleNamespace(async_url='not-a-real-db'))
    monkeypatch.setattr(benchmark,'database_snapshot_url',AsyncMock(return_value=Snapshot()))
    monkeypatch.setattr(benchmark,'run_database_doctor',AsyncMock(return_value=SimpleNamespace(ok=True)))
    error=asyncio.CancelledError() if cancel else ValueError('PRIVATE_CANARY')
    monkeypatch.setattr(benchmark,'matrix',AsyncMock(side_effect=error))
    args=SimpleNamespace(output=tmp_path/'result.json',label='verification',sizes=[100],seconds=4,clients=[1],journal_records=0)
    with pytest.raises(type(error)): asyncio.run(benchmark.run(args))
    raw=args.output.read_text()
    result=json.loads(raw)
    assert result['result']==('CANCELLED' if cancel else 'FAIL') and result['main_unchanged']
    assert 'PRIVATE_CANARY' not in raw
    assert benchmark.database_snapshot_url.await_count==2


def test_output_inside_repository_refused_before_database_access(monkeypatch):
    monkeypatch.setenv('KULAI_RUN_POSTGRES_INTEGRATION','1')
    snapshot=AsyncMock()
    monkeypatch.setattr(benchmark,'database_snapshot_url',snapshot)
    with pytest.raises(ValueError):
        asyncio.run(benchmark.run(SimpleNamespace(output=benchmark.REPO/'DO_NOT_CREATE.json')))
    snapshot.assert_not_awaited()
