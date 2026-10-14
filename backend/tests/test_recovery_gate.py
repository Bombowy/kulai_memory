"""Protected journal and actual OS locks, without private database data."""
import asyncio
import os
import pytest
from kulai_memory.security import SecurityError
from kulai_memory.security_recovery.gate import RecoveryGate, locked


def test_absent_gate_never_bootstraps_or_creates_state(tmp_path):
    with pytest.raises(Exception): RecoveryGate(tmp_path/'absent')
    assert not (tmp_path/'absent').exists()


def test_network_path_refused_without_network_access():
    if os.name!='nt': pytest.skip('Windows drive policy')
    with pytest.raises(SecurityError): RecoveryGate.initialize(r'\\untrusted.invalid\share\gate')


@pytest.mark.parametrize('corruption',[b'',b'partial',b'{}\n',b'\xff\n'])
def test_corrupt_gate_fails_closed(tmp_path,corruption):
    gate=RecoveryGate.initialize(tmp_path/'gate')
    gate.journal.write_bytes(corruption)
    with pytest.raises(SecurityError): gate.read()


def test_gate_journal_preserves_history_detects_edit_and_disallows_repo():
    from kulai_memory.security_recovery.gate import REPO
    with pytest.raises(SecurityError): RecoveryGate.initialize(REPO/'DO_NOT_CREATE')


def test_shared_leases_block_recovery_exclusive_and_release(tmp_path):
    async def run():
        gate=RecoveryGate.initialize(tmp_path/'gate')
        async with locked(gate.ingress,exclusive=False):
            async with locked(gate.ingress,exclusive=False):
                with pytest.raises(TimeoutError):
                    async with locked(gate.ingress,exclusive=True,timeout=.03): pytest.fail()
        async with locked(gate.ingress,exclusive=True):
            with pytest.raises(TimeoutError):
                async with locked(gate.ingress,exclusive=False,timeout=.03): pytest.fail()
        async with locked(gate.ingress,exclusive=False): pass
    asyncio.run(run())


def test_untrusted_acl_refused(tmp_path):
    gate=RecoveryGate.initialize(tmp_path/'gate')
    if os.name!='nt':
        gate.directory.chmod(0o755)
        try:
            with pytest.raises(SecurityError): RecoveryGate(gate.directory)
        finally: gate.directory.chmod(0o700)
    else:
        import subprocess
        # Isolated test directory only; never a system ACL change.
        command=['icacls.exe',str(gate.directory),'/grant','*S-1-1-0:(OI)(CI)(R)']
        assert subprocess.run(command,capture_output=True).returncode==0
        try:
            with pytest.raises(SecurityError): RecoveryGate(gate.directory)
        finally: subprocess.run(['icacls.exe',str(gate.directory),'/remove:g','*S-1-1-0'],capture_output=True,check=True)
