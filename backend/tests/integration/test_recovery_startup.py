"""A missing/unsafe seal cannot admit remote TLS even with valid DB credentials."""
import asyncio
import pytest
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_sessions_postgres import paired
from backend.tests.integration.test_remote_ingress_tls import application
from backend.tests.remote_tls_fixtures import certificates,tls_server,prepare_owned_gate
from kulai_memory.remote_ingress import RemoteIngressError
from kulai_memory.security_recovery import RecoveryGate
from kulai_memory.database_safety import database_snapshot_url


@pytest.mark.parametrize('state',['missing','closed','pending','partial'])
def test_actual_remote_startup_denies_unusable_gate(certificates,state):
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            await paired(factory)
            path=certificates.directory/'gate'
            if state!='missing':
                gate=RecoveryGate.initialize(path)
                if state=='pending': gate.append({'state':'PENDING','identity':None,'security':None})
                if state=='partial': gate.journal.write_bytes(b'partial record')
            app,_=application(target,factory,certificates.config())
            app.state._owned_gate_fixture=None  # no test-only fresh-state enrollment
            before=await database_snapshot_url(target.async_url)
            with pytest.raises(RemoteIngressError):
                async with tls_server(app): pytest.fail('Unsafe seal started a listener')
            assert await database_snapshot_url(target.async_url)==before
            if state=='missing': assert not path.exists()
    asyncio.run(run())


def test_readonly_backend_probe_reads_actual_catalog_and_never_changes_seal(certificates,monkeypatch):
    from ops.remote_access.validator import probe_backend
    from kulai_memory.security import SecurityMode
    from types import SimpleNamespace
    import kulai_memory.database_safety as safety
    import kulai_memory.settings as settings
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            await paired(factory)
            app,_=application(target,factory,certificates.config())
            await prepare_owned_gate(app)
            gate=RecoveryGate(certificates.directory/'gate')
            before=await database_snapshot_url(target.async_url)
            journal=gate.journal.read_bytes()
            with monkeypatch.context() as patch:
                patch.setattr(safety,'database_config',lambda:target)
                patch.setattr(settings,'get_settings',lambda:SimpleNamespace(kulai_security_mode=SecurityMode.SECURE))
                evidence=await probe_backend(gate.directory)
            assert evidence==dict(mode='SECURE',canonical_head='kulai_memory_0004',security_head='kulai_security_0003',
                                  revocation_available=True,gate_state='OPEN',external_seal_verified=True)
            assert gate.journal.read_bytes()==journal
            assert await database_snapshot_url(target.async_url)==before
    asyncio.run(run())
