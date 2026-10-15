import asyncio
import json
from pathlib import Path

import pytest

from kulai_memory.security_recovery.gate import RecoveryGate
from ops.remote_access.journal_status import inspect, within_budget
from ops.remote_access.validator import validate
from backend.tests.test_remote_gateway_config import deployment


@pytest.mark.parametrize('value,result',[
    (None,None), ({},None), ({'records':True,'bytes':1,'read_ms':1},None),
    ({'records':2,'bytes':600,'read_ms':5},True),
    ({'records':1001,'bytes':400000,'read_ms':10},False),
    ({'records':2,'bytes':16*1024*1024,'read_ms':5},False),
    ({'records':2,'bytes':600,'read_ms':50},False),
    ({'records':2,'bytes':600,'read_ms':float('nan')},False),
])
def test_review_budget_is_explicit_and_missing_data_never_pass(value,result):
    assert within_budget(value) is result


def test_journal_monitor_is_read_only_and_cannot_attest_database(tmp_path):
    gate = RecoveryGate.initialize(tmp_path/'gate')
    before = gate.journal.read_bytes()
    value = asyncio.run(inspect(gate.directory))
    assert value['result'] == 'NOT VERIFIED' and not value['production_go']
    assert value['checkpoint_matches_database'] == 'NOT VERIFIED'
    assert gate.journal.read_bytes() == before
    assert 'checksum' not in json.dumps(value) and 'identity' not in json.dumps(value)


def test_over_budget_inventory_blocks_deployment_review():
    result = validate(deployment(),dict(journal=dict(records=40000,bytes=16*1024*1024,read_ms=360)))
    assert result['result'] == 'BLOCKED'
    assert dict((c['name'],c['result']) for c in result['checks'])['journal_capacity_and_latency_budget'] == 'FAIL'
