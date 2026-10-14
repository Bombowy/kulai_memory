"""Real pg_dump/pg_restore, actual TLS and owned databases only."""
import asyncio
import json
from pathlib import Path
from dataclasses import asdict

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_sessions_postgres import paired, renew
from backend.tests.integration.test_remote_ingress_tls import application, client, headers
from backend.tests.remote_tls_fixtures import certificates, tls_server, prepare_owned_gate
from scripts.db_backup_restore_drill import seed_owned_source
from kulai_memory.backup_service import create_verified_backup
from kulai_memory.database_safety import (database_snapshot_url, create_owned_temporary_database,
    restore_archive_to_owned_database, drop_owned_temporary_database, database_config_for_database)
from kulai_memory.security import Authenticator, DeviceStatus, SecurityError, Action
from kulai_memory.security_persistence.sessions import PostgresSessionStore
from kulai_memory.security_recovery import RecoveryGate
from kulai_memory.security_recovery.gate import checkpoint
from kulai_memory.security_recovery.recovery import BackupEvidence, recover, release, canonical_part
from kulai_memory.remote_ingress import RemoteIngressError
from kulai_memory.pairing.domain import PairingError


def test_old_backup_restored_permissions_gate_recovery_and_new_pairing(certificates, caplog):
    async def run():
        async with owned_store() as (owned, config, target, factory, _):
            await seed_owned_source(owned, config=config, dimension=1024)
            store, _, _, _, old = await paired(factory, actions=frozenset(Action))
            app, _ = application(target, factory, certificates.config())
            await prepare_owned_gate(app)
            gate = RecoveryGate(certificates.directory/'gate')
            # Exercise all live pairing states without breaking their binding constraints.
            from kulai_memory.pairing.domain import PairingService
            from kulai_memory.security.authentication import utc_now
            from uuid import uuid4
            import secrets
            pairing = PairingService(store, clock=utc_now)
            invitations=[]
            for state in ('invited','pending','approved'):
                invitation=await pairing.invite(actions=frozenset({Action.MEMORY_READ}))
                proof=secrets.token_urlsafe(32)
                if state!='invited':
                    view=await pairing.request(invitation.session_id, invitation.secret, proof, uuid4(), 'Synthetic recovery')
                    if state=='approved': await store.decide(view.session_id,view.device_id,approve=True)
                invitations.append((invitation,proof,state))
            backup=await create_verified_backup(certificates.directory/'old.dump',config=config,owned=owned,app_env='test')
            evidence=BackupEvidence.from_backup(backup)
            manifest=certificates.directory/'manifest.json'
            evidence.write_manifest(manifest)
            saved=json.loads(manifest.read_text(encoding='utf-8'))
            saved['archive']=Path(saved['archive'])
            evidence=BackupEvidence(**saved)
            before=canonical_part(asdict(backup.source_snapshot))
            await store.set_device_status(old.device_id,DeviceStatus.REVOKED)
            with pytest.raises(SecurityError): await Authenticator(store).authenticate(old.access_token)
            revoked=gate.read()
            restored=await create_owned_temporary_database(kind='restore',config=config)
            engine=None
            try:
                await restore_archive_to_owned_database(evidence.archive,restored,config=config)
                restore_config=database_config_for_database(restored.name,config=config)
                engine=create_async_engine(restore_config.async_url,echo=False,hide_parameters=True)
                restore_factory=async_sessionmaker(engine,expire_on_commit=False)
                unsealed_store=PostgresSessionStore(restore_factory)
                # Threat demonstrated: DB alone resurrected a previously revoked bearer.
                assert await Authenticator(unsealed_store).authenticate(old.access_token)
                async with restore_factory() as session:
                    _, fingerprint=await checkpoint(await session.connection())
                assert fingerprint!=revoked['security']
                with pytest.raises(SecurityError): await gate.check(restore_factory)
                assert gate.read()['state']=='CLOSED'
                remote,_=application(restore_config,restore_factory,certificates.config())
                with pytest.raises(RemoteIngressError):
                    async with tls_server(remote): pytest.fail('Restored credentials opened ingress')
                result=await recover(restore_config,gate,evidence)
                assert result=={'recovered':True,'idempotent':False}
                recovered=asdict(await database_snapshot_url(restore_config.async_url))
                assert canonical_part(recovered)==before
                assert recovered['security']['devices']['count']==backup.source_snapshot.security.devices.count
                assert await recover(restore_config,gate,evidence)=={'recovered':True,'idempotent':True}
                assert asdict(await database_snapshot_url(restore_config.async_url))==recovered
                assert gate.read()['state']=='RECOVERED'
                with pytest.raises(SecurityError): await Authenticator(unsealed_store).authenticate(old.access_token)
                await release(restore_config,gate)
                restore_factory.configure(info={'recovery_gate':gate})
                for invite,proof,state in invitations:
                    with pytest.raises(PairingError):
                        if state=='invited': await PairingService(unsealed_store,clock=utc_now).request(invite.session_id,invite.secret,proof,uuid4(),'Synthetic replay')
                        else: await PairingService(unsealed_store,clock=utc_now).claim(invite.session_id,proof)
                assert (await renew(unsealed_store,old)).credential is None
                # Rejected proofs may update existing abuse-rate audit counters;
                # all such writes participate in the external checkpoint.
                await gate.check(restore_factory)
                remote,_=application(restore_config,restore_factory,certificates.config())
                async with tls_server(remote) as (port,_):
                    async with client(certificates,port) as transport:
                        assert (await transport.get('/api/memories',headers=headers(old))).status_code==401
                    new_store,_,_,_,new=await paired(restore_factory,actions=frozenset(Action))
                    assert new.device_id!=old.device_id
                    async with client(certificates,port) as transport:
                        assert (await transport.get('/api/memories',headers=headers(new))).status_code==200
                    with pytest.raises(TimeoutError): await recover(restore_config,gate,evidence)
                final=asdict(await database_snapshot_url(restore_config.async_url))
                assert canonical_part(final)==before
                assert old.access_token not in caplog.text and old.refresh_credential not in caplog.text
            finally:
                if engine: await engine.dispose()
                await drop_owned_temporary_database(restored,config=config)
    asyncio.run(asyncio.wait_for(run(),180))


def test_gate_uncertain_commit_and_rollback_fail_closed(certificates, monkeypatch):
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            store,_,_,_,credential=await paired(factory)
            app,_=application(target,factory,certificates.config())
            await prepare_owned_gate(app)
            gate=RecoveryGate(certificates.directory/'gate')
            before=gate.read()
            with pytest.raises(RuntimeError):
                async with gate.transaction(factory) as session:
                    await session.execute(text("UPDATE kulai_security_devices SET status='revoked'"))
                    raise RuntimeError('Synthetic before commit')
            assert gate.read()['state']=='OPEN'
            await gate.check(factory)
            original=gate.append
            def fail_after_commit(record,**kw):
                if record['state']=='OPEN': raise SecurityError()
                return original(record,**kw)
            monkeypatch.setattr(gate,'append',fail_after_commit)
            with pytest.raises(SecurityError):
                async with gate.transaction(factory) as session:
                    await session.execute(text("UPDATE kulai_security_devices SET status='revoked'"))
            assert gate.read()['state']=='PENDING'
            with pytest.raises(SecurityError): await gate.check(factory)
            # COMMIT happened, response/checkpoint did not; no automatic reopen.
            async with factory() as session:
                assert await session.scalar(text('SELECT status FROM kulai_security_devices WHERE id=:id'),{'id':credential.device_id})=='revoked'
    asyncio.run(run())


def test_corrupt_archive_closes_gate_without_security_write(certificates):
    async def run():
        async with owned_store() as (owned,config,target,factory,_):
            await paired(factory)
            app,_=application(target,factory,certificates.config())
            await prepare_owned_gate(app)
            gate=RecoveryGate(certificates.directory/'gate')
            backup=await create_verified_backup(certificates.directory/'bad.dump',config=config,owned=owned,app_env='test')
            evidence=BackupEvidence.from_backup(backup)
            before=await database_snapshot_url(target.async_url)
            evidence.archive.write_bytes(b'corrupted synthetic archive')
            with pytest.raises(SecurityError): await recover(target,gate,evidence)
            assert gate.read()['state']=='CLOSED'
            assert await database_snapshot_url(target.async_url)==before
            with pytest.raises(SecurityError): await release(target,gate)
    asyncio.run(run())


@pytest.mark.parametrize('failure',['middle_update','commit_response'])
def test_recovery_atomicity_and_unknown_commit_require_manual_recovery(certificates,monkeypatch,failure):
    from sqlalchemy.ext.asyncio import AsyncSession
    async def run():
        async with owned_store() as (owned,config,target,factory,_):
            _,_,_,_,credential=await paired(factory)
            app,_=application(target,factory,certificates.config())
            await prepare_owned_gate(app)
            gate=RecoveryGate(certificates.directory/'gate')
            evidence=BackupEvidence.from_backup(await create_verified_backup(certificates.directory/'atomic.dump',config=config,owned=owned,app_env='test'))
            before=await database_snapshot_url(target.async_url)
            original_execute=AsyncSession.execute
            original_commit=AsyncSession.commit
            async def execute(session,statement,*a,**kw):
                if str(statement).startswith('UPDATE public.kulai_security_tokens'):
                    raise RuntimeError('Synthetic mid-operation failure')
                return await original_execute(session,statement,*a,**kw)
            async def commit(session):
                await original_commit(session)
                raise RuntimeError('Synthetic lost COMMIT response')
            with monkeypatch.context() as fault:
                fault.setattr(AsyncSession,'execute' if failure=='middle_update' else 'commit',execute if failure=='middle_update' else commit)
                with pytest.raises(RuntimeError): await recover(target,gate,evidence)
            assert gate.read()['state']=='CLOSED'
            after=await database_snapshot_url(target.async_url)
            if failure=='middle_update': assert after==before
            else:
                assert canonical_part(asdict(after))==canonical_part(asdict(before))
                with pytest.raises(SecurityError): await Authenticator(PostgresSessionStore(factory)).authenticate(credential.access_token)
            with pytest.raises(SecurityError): await release(target,gate)
            # Explicit retry of recovery (never a blind retry of COMMIT) starts
            # from observed DB state, verifies the archive again, and is safe.
            await recover(target,gate,evidence)
            assert gate.read()['state']=='RECOVERED'
    asyncio.run(asyncio.wait_for(run(),120))
