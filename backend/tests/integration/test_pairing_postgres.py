"""Pairing, races and restoration use only process-owned synthetic databases."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import secrets
import sys
from uuid import UUID, uuid4
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_security_uvicorn import server
from backend.tests.test_security_transport import Runtime, HTTP, WS, no_private_work
from kulai_memory.pairing.domain import PairingService, PairingError, PairingState, digest
from kulai_memory.security_persistence.pairing import PostgresPairingStore
from kulai_memory.security.authentication import utc_now
from kulai_memory.security import Action, Authenticator, CredentialManager, SecurityError
from kulai_memory.app_factory import create_app
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime
from kulai_memory.database_safety import database_snapshot_url, run_database_doctor


async def pending(factory, *, actions=frozenset(Action)):
    store = PostgresPairingStore(factory)
    service = PairingService(store, clock=utc_now)
    invite = await service.invite(actions=actions)
    proof, client = secrets.token_urlsafe(32), uuid4()
    view = await service.request(invite.session_id, invite.secret, proof, client, 'Android test')
    return store, service, invite, proof, client, view


def test_pending_approval_claim_restart_and_secrets_absent():
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            assert (await run_database_doctor(async_url=target.async_url, require_security=True)).ok
            store, service, invite, proof, client, view = await pending(factory, actions=frozenset({Action.MEMORY_READ}))
            assert view.state is PairingState.PENDING and view.client_id == client and view.device_id != client
            assert repr(invite).find(invite.secret) == -1
            with pytest.raises(PairingError):
                await service.claim(invite.session_id, proof)
            assert (await service.request(invite.session_id, invite.secret, proof, client, 'Android test')) == view
            with pytest.raises(SecurityError):
                await store.decide(invite.session_id, uuid4(), approve=True)
            await store.decide(invite.session_id, view.device_id, approve=True)
            await store.decide(invite.session_id, view.device_id, approve=True)
            restarted = PairingService(PostgresPairingStore(factory), clock=utc_now)
            assert (await restarted.status(invite.session_id, proof)).state is PairingState.APPROVED
            token = await restarted.claim(invite.session_id, proof)
            principal = await Authenticator(store).authenticate(token.access_token, claimed_device_id=view.device_id)
            assert principal.actions == frozenset({Action.MEMORY_READ})
            with pytest.raises(PairingError):
                await restarted.claim(invite.session_id, proof)
            async with factory() as session:
                data = await session.scalar(sa.text('SELECT json_agg(p)::text FROM public.kulai_security_pairings p'))
                credentials = await session.scalar(sa.text('SELECT json_agg(t)::text FROM public.kulai_security_tokens t'))
                assert not any(secret in data + credentials for secret in (invite.secret, proof, token.access_token))
            await CredentialManager(store).revoke_device(view.device_id)
            assert (await restarted.status(invite.session_id, proof)).state is PairingState.REVOKED
            with pytest.raises(SecurityError):
                await Authenticator(store).authenticate(token.access_token)
            assert factory.kw['bind'].pool.checkedout() == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal', ['deny', 'cancel', 'expire', 'attempts'])
def test_refusal_expiry_and_durable_attempt_budget(terminal):
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, service, invite, proof, _, view = await pending(factory)
            if terminal == 'deny':
                await store.decide(invite.session_id, view.device_id, approve=False)
                await store.decide(invite.session_id, view.device_id, approve=False)
            elif terminal == 'cancel':
                await service.status(invite.session_id, proof, cancel=True)
                assert (await service.status(invite.session_id, proof, cancel=True)).state is PairingState.CANCELLED
            elif terminal == 'expire':
                async with factory() as session:
                    await session.execute(sa.text("UPDATE public.kulai_security_pairings SET created_at=created_at-interval '6 minutes', expires_at=expires_at-interval '6 minutes'"))
                    await session.commit()
                assert (await service.status(invite.session_id, proof)).state is PairingState.EXPIRED
            else:
                for _ in range(8):
                    with pytest.raises(PairingError):
                        await service.status(invite.session_id, secrets.token_urlsafe(32))
                with pytest.raises(PairingError):
                    await service.status(invite.session_id, proof)
            with pytest.raises((PairingError, SecurityError)):
                await store.decide(invite.session_id, view.device_id, approve=True)
            with pytest.raises((PairingError, SecurityError)):
                await service.claim(invite.session_id, proof)
    asyncio.run(scenario())


def test_limits_unknown_invitation_substitution_and_concurrent_registration_claim():
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, service, invite, proof, client, view = await pending(factory)
            with pytest.raises(PairingError):
                await service.request(invite.session_id, invite.secret, secrets.token_urlsafe(32), client, 'Android test')
            with pytest.raises(PairingError):
                await service.request(uuid4(), invite.secret, proof, client, 'Android test')
            await store.decide(invite.session_id, view.device_id, approve=True)
            results = await asyncio.gather(*(service.claim(invite.session_id, proof) for _ in range(8)), return_exceptions=True)
            assert sum(not isinstance(r, Exception) for r in results) == 1
            for _ in range(8):
                await service.invite(actions=frozenset(Action))
            with pytest.raises(SecurityError):
                await service.invite(actions=frozenset(Action))
            async with factory() as session:
                await session.execute(sa.text('UPDATE public.kulai_security_pairing_rate SET attempts=120'))
                await session.commit()
            with pytest.raises(PairingError):
                await service.status(invite.session_id, proof)
    asyncio.run(scenario())


def test_claim_commit_failure_full_rollback_without_secret_diagnostics(caplog):
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, service, invite, proof, _, view = await pending(factory)
            await store.decide(invite.session_id, view.device_id, approve=True)
            class FaultSession(AsyncSession):
                pass
            def fail(session):
                raise RuntimeError('PRIVATE_PAIRING_FAULT')
            sa.event.listen(FaultSession.sync_session_class, 'before_commit', fail)
            try:
                faulty = PairingService(PostgresPairingStore(async_sessionmaker(factory.kw['bind'], class_=FaultSession)), clock=utc_now)
                with pytest.raises(SecurityError) as caught:
                    await faulty.claim(invite.session_id, proof)
                assert str(caught.value) == 'Authentication required.' and caught.value.__cause__ is None
            finally:
                sa.event.remove(FaultSession.sync_session_class, 'before_commit', fail)
            assert (await service.status(invite.session_id, proof)).state is PairingState.APPROVED
            async with factory() as session:
                assert await session.scalar(sa.text('SELECT count(*) FROM public.kulai_security_tokens')) == 0
            assert 'PRIVATE_PAIRING_FAULT' not in caplog.text
            await service.claim(invite.session_id, proof)
    asyncio.run(scenario())


def test_real_http_pairing_with_managed_lifecycle_and_private_denials(capsys):
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            store = PostgresPairingStore(factory)
            service = PairingService(store, clock=utc_now)
            invitation = await service.invite(actions=frozenset(Action))
            proof = secrets.token_urlsafe(32)
            runtime = Runtime()
            owner = PostgresSecurityRuntime(config_factory=lambda: target)
            app = create_app(voice_runtime_factory=lambda: runtime, security_runtime_factory=lambda: owner)
            async with server(app) as port:
                async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=5) as client:
                    payload = dict(session_id=str(invitation.session_id), receiver_secret=proof, client_id=str(uuid4()), device_label='Android test')
                    request = await client.post('/api/pairing/request', json=payload, headers={'Authorization': 'Pairing ' + invitation.secret})
                    assert request.status_code == 200, request.text
                    device = request.json()['device_id']
                    for method, route in HTTP:
                        assert (await client.request(method, route)).status_code == 401
                    no_private_work(runtime)
                    assert (await client.post('/api/pairing/claim', json={'session_id': payload['session_id']}, headers={'Authorization': 'Pairing ' + proof})).status_code == 400
                    await store.decide(invitation.session_id, UUID(device), approve=True)
                    claimed = await client.post('/api/pairing/claim', json={'session_id': payload['session_id']}, headers={'Authorization': 'Pairing ' + proof})
                    assert claimed.status_code == 200
                    token = claimed.json()['access_token']
                    assert (await client.get('/api/memories', headers={'Authorization': 'Bearer ' + token, 'X-Kulai-Device-ID': device})).status_code == 200
                    for route in ('/api/pairing/request/extra', '/api/pairing/status/', '/api/pairing/approve'):
                        assert (await client.post(route)).status_code == 401
                    assert (await client.post('/api/pairing/status?secret=forbidden-query', json={})).status_code == 403
                    assert (await client.post('/api/pairing/status', json={}, headers={'X-Forwarded-For': '127.0.0.1'})).status_code == 403
                    bad = await client.post('/api/pairing/status', content='{"session_id":"PRIVATE_BODY","session_id":"x"}', headers={'Authorization': 'Pairing ' + proof, 'Content-Type': 'application/json'})
                    assert bad.status_code == 400 and 'PRIVATE_BODY' not in bad.text
            output = capsys.readouterr()
            assert all(secret not in output.out + output.err for secret in (invitation.secret, proof, token))
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['pairing_request', 'pairing_claim'])
def test_two_process_pairing_race_is_single_use(operation):
    async def scenario():
        async with owned_store() as (owned, _, _, factory, _):
            store = PostgresPairingStore(factory)
            service = PairingService(store, clock=utc_now)
            invite = await service.invite(actions=frozenset(Action))
            proof, client_id = secrets.token_urlsafe(32), uuid4()
            if operation == 'pairing_claim':
                view = await service.request(invite.session_id, invite.secret, proof, client_id, 'Android test')
                await store.decide(invite.session_id, view.device_id, approve=True)
            children = []
            try:
                for index in range(2):
                    child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'backend.tests.security_postgres_worker',
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    children.append(child)
                    data = dict(database=owned.name, marker=owned.marker, operation=operation, session_id=str(invite.session_id),
                        invitation=invite.secret, proof=proof if operation == 'pairing_claim' or index == 0 else secrets.token_urlsafe(32), client_id=str(client_id))
                    child.stdin.write((json.dumps(data) + '\n').encode()); await child.stdin.drain()
                for child in children:
                    assert (await asyncio.wait_for(child.stdout.readline(), 10)).strip() == b'READY'
                for child in children:
                    child.stdin.write(b'GO\n'); await child.stdin.drain()
                results = []
                for child in children:
                    output, errors = await asyncio.wait_for(child.communicate(), 10)
                    assert child.returncode == 0 and not errors
                    assert invite.secret.encode() not in output and proof.encode() not in output
                    results.append(json.loads(output))
                assert sum(result['ok'] for result in results) == 1
            finally:
                for child in children:
                    if child.returncode is None:
                        child.kill(); await child.wait()
    asyncio.run(scenario())


def test_v1_upgrade_and_full_pairing_backup_restore_preserve_nonempty_canonical(tmp_path):
    async def scenario():
        async with owned_store(security=False) as (owned, config, target, factory, _):
            from kulai_memory.security_persistence import migration
            from kulai_memory.backup_service import create_verified_backup, revalidate_verified_backup
            from scripts.db_backup_restore_drill import seed_owned_source
            await seed_owned_source(owned, config=config, dimension=1024)
            async with factory.kw['bind'].begin() as connection:
                await connection.run_sync(lambda conn: migration._upgrade(conn, 'kulai_security_0001'))
            store = PostgresPairingStore(factory)
            manager = CredentialManager(store)
            device = await manager.register_device(trusted=True)
            original = await manager.issue(device.id)
            before = await database_snapshot_url(target.async_url)
            assert before.security.revision == ('kulai_security_0001',) and before.security.pairings is None
            owner = PostgresSecurityRuntime(config_factory=lambda: target)
            await owner.startup()
            assert owner.ready and owner.pairing_service is None
            await owner.shutdown()
            backup = await migration.upgrade_security_with_verified_backup(tmp_path / 'before-pairing.dump', config=config, app_env='test', owned=owned)
            assert backup.source_snapshot == before
            after = await database_snapshot_url(target.async_url)
            assert after.security.devices == before.security.devices and after.security.tokens == before.security.tokens
            assert (after.memories, after.vectors, after.tombstones) == (before.memories, before.vectors, before.tombstones)
            assert (await Authenticator(store).authenticate(original.access_token)).device_id == device.id
            store, service, invite, proof, _, view = await pending(factory)
            await store.decide(invite.session_id, view.device_id, approve=True)
            token = await service.claim(invite.session_id, proof)
            await manager.revoke(token.token_id)
            full = await database_snapshot_url(target.async_url)
            assert full.security.pairings.count == 1 and full.security.pairing_rate.count == 1
            durable = await create_verified_backup(tmp_path / 'paired.dump', config=config, app_env='test', owned=owned)
            assert durable.source_snapshot == full
            await revalidate_verified_backup(durable, config=target)
            # Entire nonempty security and canonical snapshot survived real pg_restore.
            assert (full.memories.count, full.vectors.count, full.tombstones.count) == (2, 1, 1)
    asyncio.run(scenario())


def test_revoke_claim_race_never_restores_trust():
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, service, invite, proof, _, view = await pending(factory)
            await store.decide(invite.session_id, view.device_id, approve=True)
            result = await asyncio.gather(service.claim(invite.session_id, proof),
                CredentialManager(store).revoke_device(view.device_id), return_exceptions=True)
            assert result[1] is None
            if not isinstance(result[0], Exception):
                with pytest.raises(SecurityError):
                    await Authenticator(store).authenticate(result[0].access_token)
            with pytest.raises((SecurityError, PairingError)):
                await service.claim(invite.session_id, proof)
    asyncio.run(scenario())


@pytest.mark.parametrize('tamper', ['constraint', 'index', 'partial'])
def test_pairing_schema_tamper_fails_doctor_and_snapshot(tamper):
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            statements = {'constraint': 'ALTER TABLE public.kulai_security_pairings DROP CONSTRAINT ck_pairing_claim',
                'index': 'DROP INDEX public.ix_security_pairings_expiry', 'partial': 'DROP TABLE public.kulai_security_pairing_rate'}
            async with factory() as session:
                await session.execute(sa.text(statements[tamper])); await session.commit()
            assert not (await run_database_doctor(async_url=target.async_url)).ok
            with pytest.raises(Exception, match='snapshot could not be read safely'):
                await database_snapshot_url(target.async_url)
    asyncio.run(scenario())


def test_admin_cli_explicit_invite_target_approval_revoke_and_no_token_output(monkeypatch, capsys):
    async def scenario():
        from scripts import pairing_admin as admin
        async with owned_store() as (_, _, target, factory, _):
            monkeypatch.setattr(admin, 'database_config', lambda: target)
            parser = admin.parser()
            assert await admin.run(parser.parse_args(['invite', '--show-secret', '--permissions', 'memory.read'])) == 0
            printed = capsys.readouterr()
            invitation = json.loads(printed.out)
            service = PairingService(PostgresPairingStore(factory), clock=utc_now)
            proof = secrets.token_urlsafe(32)
            view = await service.request(UUID(invitation['session_id']), invitation['invitation_secret'], proof, uuid4(), 'Android test')
            assert await admin.run(parser.parse_args(['pending'])) == 0
            pending_output = json.loads(capsys.readouterr().out)
            assert pending_output[0]['verification_code'] == view.verification_code
            base = ['approve', '--session-id', str(view.session_id), '--device-id', str(view.device_id), '--verification-code']
            with pytest.raises(PairingError): await admin.run(parser.parse_args(base + ['incorrect']))
            assert await admin.run(parser.parse_args(base + [view.verification_code])) == 0
            token = await service.claim(view.session_id, proof)
            assert await admin.run(parser.parse_args(['devices'])) == 0
            assert await admin.run(parser.parse_args(['revoke', '--device-id', str(view.device_id)])) == 0
            assert token.access_token not in capsys.readouterr().out
            with pytest.raises(SecurityError): await Authenticator(PostgresPairingStore(factory)).authenticate(token.access_token)
    asyncio.run(scenario())


def test_expired_pruning_releases_capacity_but_keeps_live_credentials():
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, service, invite, proof, _, view = await pending(factory)
            await store.decide(view.session_id, view.device_id, approve=True)
            token = await service.claim(view.session_id, proof)
            await service.invite(actions=frozenset(Action))
            async with factory() as session:
                await session.execute(sa.text("UPDATE public.kulai_security_pairings SET created_at=created_at-interval '6 minutes', expires_at=expires_at-interval '6 minutes'")); await session.commit()
            assert await store.prune_expired() == 1
            assert await Authenticator(store).authenticate(token.access_token)
            await CredentialManager(store).revoke_device(view.device_id)
            assert await store.prune_expired() == 1
    asyncio.run(scenario())


def test_real_disconnect_cancels_blocked_pairing_transaction_and_releases_pool():
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            store = PostgresPairingStore(factory)
            invite = await PairingService(store, clock=utc_now).invite(actions=frozenset(Action))
            owner = PostgresSecurityRuntime(config_factory=lambda: target)
            runtime = Runtime()
            app = create_app(voice_runtime_factory=lambda: runtime, security_runtime_factory=lambda: owner)
            async with server(app) as port:
                async with factory() as blocker:
                    await blocker.execute(sa.text('SELECT pg_advisory_xact_lock(49142002)'))
                    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=8) as client:
                        payload = {'session_id':str(invite.session_id), 'receiver_secret':secrets.token_urlsafe(32),
                            'client_id':str(uuid4()), 'device_label':'Android test'}
                        request = asyncio.create_task(client.post('/api/pairing/request', json=payload,
                            headers={'Authorization':'Pairing '+invite.secret}))
                        for _ in range(100):
                            if owner._engine.pool.checkedout() == 1: break
                            await asyncio.sleep(0.01)
                        assert owner._engine.pool.checkedout() == 1
                        request.cancel()
                        with pytest.raises(asyncio.CancelledError): await request
                        for _ in range(100):
                            if owner._engine.pool.checkedout() == 0: break
                            await asyncio.sleep(0.01)
                        assert owner._engine.pool.checkedout() == 0
                        no_private_work(runtime)
                        await blocker.rollback()
                        response = await client.post('/api/pairing/request', json=payload, headers={'Authorization':'Pairing '+invite.secret})
                        assert response.status_code == 200
    asyncio.run(scenario())
