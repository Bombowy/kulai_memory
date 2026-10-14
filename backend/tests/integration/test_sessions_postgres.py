"""Real PostgreSQL sessions, replay and recovery on capability-owned databases only."""
import asyncio
from datetime import timedelta
import secrets
import json
import sys
from uuid import uuid4
import pytest
import sqlalchemy as sa
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from backend.tests.security_postgres_fixtures import owned_store
from kulai_memory.database_safety import run_database_doctor
from kulai_memory.pairing.domain import PairingService, PairingState
from kulai_memory.security.authentication import utc_now, token_digest
from kulai_memory.security_persistence.postgres import PostgresSecurityStore
from kulai_memory.security import Action, Authenticator, CredentialManager, SecurityError
from kulai_memory.security.sessions import SessionService, refresh_digest
from kulai_memory.security_persistence.sessions import PostgresSessionStore
from backend.tests.integration.test_security_uvicorn import server
from backend.tests.test_security_transport import Runtime, HTTP, WS, no_private_work
from kulai_memory.app_factory import create_app
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime


async def paired(factory, *, actions=frozenset({Action.MEMORY_READ}), ttl=timedelta(hours=1)):
    store = PostgresSessionStore(factory)
    pairing = PairingService(store, clock=utc_now)
    invite = await pairing.invite(actions=actions, token_ttl=ttl)
    receiver = secrets.token_urlsafe(32)
    view = await pairing.request(invite.session_id, invite.secret, receiver, uuid4(), 'Session test')
    await store.decide(view.session_id, view.device_id, approve=True)
    return store, pairing, invite, receiver, await pairing.claim(view.session_id, receiver)


async def age_refresh(factory):
    # Synthetic rows only; retain CHECK validity while bypassing real 30s wait.
    store = PostgresSessionStore(factory)
    async with store._transaction() as session:
        await session.execute(sa.text("UPDATE public.kulai_security_refreshes SET issued_at=issued_at-interval '31 seconds', expires_at=expires_at-interval '31 seconds'"))


async def renew(store, credential):
    return await SessionService(store).renew(credential.session_id, credential.device_id,
        credential.refresh_id, credential.refresh_credential)


def test_durable_session_atomic_rotation_replay_and_schema():
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            assert (await run_database_doctor(async_url=target.async_url, require_security=True)).ok
            store, pairing, invite, receiver, initial = await paired(factory)
            assert initial.refresh_credential not in repr(initial)
            with pytest.raises(SecurityError):
                await CredentialManager(store).rotate(initial.access_token, device_id=initial.device_id)
            async with factory() as session:
                rows = (await session.execute(sa.text('SELECT json_agg(r)::text FROM public.kulai_security_refreshes r'))).scalar()
                assert initial.refresh_credential not in rows and initial.access_token not in rows
            await age_refresh(factory)
            rotated = (await renew(PostgresSessionStore(factory), initial)).credential
            assert rotated is not None and rotated.session_expires_at == initial.session_expires_at
            assert rotated.refresh_id != initial.refresh_id and rotated.token_id != initial.token_id
            with pytest.raises(SecurityError):
                await Authenticator(store).authenticate(initial.access_token)
            assert (await Authenticator(store).authenticate(rotated.access_token)).actions == frozenset({Action.MEMORY_READ})
            assert (await pairing.status(invite.session_id, receiver)).state is PairingState.CLAIMED
            assert (await renew(PostgresSessionStore(factory), initial)).credential is None
            with pytest.raises(SecurityError):
                await Authenticator(PostgresSessionStore(factory)).authenticate(rotated.access_token)
            assert (await renew(PostgresSessionStore(factory), rotated)).credential is None
            assert (await pairing.status(invite.session_id, receiver)).state is PairingState.REVOKED
            assert factory.kw['bind'].pool.checkedout() == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['secret', 'device', 'refresh_id', 'session_id', 'refresh_expiry', 'absolute_expiry', 'device_revoke', 'scope', 'access_revoke'])
def test_invalid_expired_revoked_or_inconsistent_family_denies(case):
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, _, _, _, credential = await paired(factory)
            await age_refresh(factory)
            sid, did, rid, secret = credential.session_id, credential.device_id, credential.refresh_id, credential.refresh_credential
            if case == 'secret': secret = 'krf1_'+secrets.token_urlsafe(32)
            if case == 'device': did = uuid4()
            if case == 'session_id': sid = uuid4()
            if case == 'refresh_id': rid = uuid4()
            if case in ('refresh_expiry', 'absolute_expiry', 'scope'):
                sql = {
                    'refresh_expiry': "UPDATE public.kulai_security_refreshes SET issued_at=issued_at-interval '31 days', expires_at=expires_at-interval '31 days'",
                    'absolute_expiry': "UPDATE public.kulai_security_sessions SET created_at=created_at-interval '91 days', absolute_expires_at=absolute_expires_at-interval '91 days'",
                    'scope': "UPDATE public.kulai_security_sessions SET actions=ARRAY['memory.read','memory.write']::text[]",
                }[case]
                async with factory.begin() as session: await session.execute(sa.text(sql))
            if case == 'device_revoke': await CredentialManager(store).revoke_device(did)
            if case == 'access_revoke': await CredentialManager(store).revoke(credential.token_id)
            assert (await SessionService(store).renew(sid, did, rid, secret)).credential is None
            async with factory() as session:
                assert await session.scalar(sa.text('SELECT count(*) FROM public.kulai_security_refreshes')) == 1
            if case in ('secret','device','session_id','refresh_id'):
                assert (await renew(store, credential)).credential is not None  # Bad proof cannot revoke another family.
    asyncio.run(scenario())


def test_access_expiry_allows_only_refresh_and_clamps_absolute_lifetime():
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, _, _, _, initial = await paired(factory)
            await age_refresh(factory)
            async with factory.begin() as session:
                await session.execute(sa.text("UPDATE public.kulai_security_tokens SET issued_at=issued_at-interval '2 hours', expires_at=expires_at-interval '2 hours'"))
                await session.execute(sa.text("UPDATE public.kulai_security_sessions SET absolute_expires_at=clock_timestamp()+interval '2 minutes'"))
            with pytest.raises(SecurityError): await Authenticator(store).authenticate(initial.access_token)
            result = (await renew(store, initial)).credential
            assert result is not None and result.expires_at == result.session_expires_at == result.refresh_expires_at
            assert (await Authenticator(store).authenticate(result.access_token)).actions == frozenset({Action.MEMORY_READ})
    asyncio.run(scenario())


@pytest.mark.parametrize('operations', [('renew','renew'), ('renew','revoke_device')])
def test_two_process_renewal_replay_and_device_revoke(operations):
    async def scenario():
        async with owned_store() as (owned, _, _, factory, _):
            store, _, _, _, initial = await paired(factory)
            await age_refresh(factory)
            children=[]
            try:
                for operation in operations:
                    child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'backend.tests.security_postgres_worker',
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    children.append(child)
                    data = dict(database=owned.name, marker=owned.marker, operation=operation,
                        session_id=str(initial.session_id), device_id=str(initial.device_id),
                        refresh_id=str(initial.refresh_id), refresh=initial.refresh_credential)
                    child.stdin.write((json.dumps(data)+'\n').encode()); await child.stdin.drain()
                for child in children: assert (await asyncio.wait_for(child.stdout.readline(),10)).strip() == b'READY'
                for child in children: child.stdin.write(b'GO\n'); await child.stdin.drain()
                results=[]
                for child in children:
                    output, errors = await asyncio.wait_for(child.communicate(),10)
                    assert child.returncode == 0 and not errors and initial.refresh_credential.encode() not in output
                    results.append(json.loads(output))
                if operations == ('renew','renew'):
                    assert sum(r['ok'] for r in results) == 1
                    async with factory() as session:
                        row = (await session.execute(sa.text('SELECT generation,revoked_at FROM public.kulai_security_sessions'))).one()
                        assert row.generation == 1 and row.revoked_at is not None
                        assert await session.scalar(sa.text('SELECT count(*) FROM public.kulai_security_tokens WHERE revoked_at IS NULL')) == 0
                else:
                    assert results[1]['ok']
                    assert (await PostgresSecurityStore(factory).lookup(token_digest(initial.access_token))).device.status.value == 'revoked'
                assert (await renew(store, initial)).credential is None
            finally:
                for child in children:
                    if child.returncode is None: child.kill(); await child.wait()
    asyncio.run(asyncio.wait_for(scenario(),40))


@pytest.mark.parametrize('fault', ['before_commit','after_rollback','after_commit'])
def test_rollback_and_ambiguous_commit_never_emit_or_retry_secrets(fault, caplog):
    async def scenario():
        async with owned_store() as (_, _, _, factory, _):
            store, _, _, _, initial = await paired(factory)
            await age_refresh(factory)
            def fail(session):
                if session.info.get('fault'): raise RuntimeError(initial.refresh_credential)
            failing = async_sessionmaker(factory.kw['bind'], expire_on_commit=False, info={'fault':True})
            sa.event.listen(AsyncSession.sync_session_class, fault, fail)
            if fault == 'after_rollback':
                # Real database failure after consumption triggers a failing rollback hook.
                async with factory.begin() as session:
                    await session.execute(sa.text('ALTER TABLE public.kulai_security_tokens ADD CONSTRAINT test_no_new_tokens CHECK (revoked_at IS NULL)'))
            try:
                with pytest.raises(SecurityError) as caught: await renew(PostgresSessionStore(failing), initial)
                assert caught.value.__cause__ is None and initial.refresh_credential not in str(caught.value)
            finally: sa.event.remove(AsyncSession.sync_session_class, fault, fail)
            async with factory() as session:
                count = await session.scalar(sa.text('SELECT count(*) FROM public.kulai_security_refreshes'))
                assert count == (2 if fault == 'after_commit' else 1)
            if fault == 'after_commit':
                # Client has no replacement. Old refresh cannot recover it; replay revokes.
                assert (await renew(store, initial)).credential is None
            else: assert await Authenticator(store).authenticate(initial.access_token)
            assert factory.kw['bind'].pool.checkedout() == 0 and initial.refresh_credential not in caplog.text
    asyncio.run(scenario())


def test_full_sessions_backup_actual_restore_and_historical_v2_upgrade(tmp_path):
    async def scenario():
        from kulai_memory.security_persistence import migration
        from kulai_memory.security_persistence.pairing import PostgresPairingStore
        from kulai_memory.backup_service import create_verified_backup, revalidate_verified_backup
        from kulai_memory.database_safety import database_snapshot_url
        from scripts.db_backup_restore_drill import seed_owned_source
        async with owned_store(security=False) as (owned,config,target,factory,_):
            await seed_owned_source(owned,config=config,dimension=1024)
            async with factory.kw['bind'].begin() as connection:
                await connection.run_sync(lambda conn:migration._upgrade(conn,'kulai_security_0002'))
            legacy=PostgresPairingStore(factory)
            pairing=PairingService(legacy,clock=utc_now)
            invite=await pairing.invite(actions=frozenset({Action.MEMORY_READ}))
            proof=secrets.token_urlsafe(32)
            view=await pairing.request(invite.session_id,invite.secret,proof,uuid4(),'Historical test')
            await legacy.decide(view.session_id,view.device_id,approve=True)
            access=await pairing.claim(view.session_id,proof)
            before=await database_snapshot_url(target.async_url)
            assert before.security.revision==('kulai_security_0002',) and before.security.sessions is None
            backup=await migration.upgrade_security_with_verified_backup(tmp_path/'v2.dump',config=config,app_env='test',owned=owned)
            assert backup.source_snapshot==before
            after=await database_snapshot_url(target.async_url)
            assert (after.memories,after.vectors,after.tombstones)==(before.memories,before.vectors,before.tombstones)
            assert (after.security.devices,after.security.tokens,after.security.pairings,after.security.pairing_rate)==(
                before.security.devices,before.security.tokens,before.security.pairings,before.security.pairing_rate)
            owner=PostgresSecurityRuntime(config_factory=lambda:target)
            await owner.startup()
            assert owner.ready and owner.session_service is not None
            assert await Authenticator(owner).authenticate(access.access_token) # No invented refresh for v2 token.
            await owner.shutdown()
            store,_,_,_,initial=await paired(factory)
            await age_refresh(factory)
            rotated=(await renew(store,initial)).credential
            await renew(store,initial) # Retain replay/revocation history in dump.
            full=await database_snapshot_url(target.async_url)
            assert full.security.sessions.count==1 and full.security.refreshes.count==2
            assert (full.memories.count,full.vectors.count,full.tombstones.count)==(2,1,1)
            durable=await create_verified_backup(tmp_path/'sessions.dump',config=config,app_env='test',owned=owned)
            assert durable.source_snapshot==full
            await revalidate_verified_backup(durable,config=target)
            assert (await renew(PostgresSessionStore(factory),rotated)).credential is None
    asyncio.run(scenario())


def test_real_disconnect_during_refresh_rolls_back_and_drains_pool():
    async def scenario():
        async with owned_store() as (_,_,target,factory,_):
            store,_,_,_,initial=await paired(factory)
            await age_refresh(factory)
            runtime=Runtime(); owner=PostgresSecurityRuntime(config_factory=lambda:target)
            app=create_app(voice_runtime_factory=lambda:runtime,security_runtime_factory=lambda:owner)
            async with server(app) as port:
                async with factory() as blocker:
                    await blocker.execute(sa.text('SELECT pg_advisory_xact_lock(49142002)'))
                    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=8) as client:
                        task=asyncio.create_task(client.post('/api/security/refresh',headers={'Authorization':'Refresh '+initial.refresh_credential},
                            json=dict(session_id=str(initial.session_id),device_id=str(initial.device_id),refresh_id=str(initial.refresh_id))))
                        for _ in range(100):
                            if owner._engine.pool.checkedout()==1: break
                            await asyncio.sleep(.01)
                        assert owner._engine.pool.checkedout()==1
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError): await task
                        for _ in range(100):
                            if owner._engine.pool.checkedout()==0: break
                            await asyncio.sleep(.01)
                        assert owner._engine.pool.checkedout()==0
                        await blocker.rollback()
            # Test knows rollback; production client cannot infer it and does not retry.
            assert (await renew(store,initial)).credential is not None
            no_private_work(runtime)
    asyncio.run(scenario())


def test_real_refresh_http_managed_lifecycle_denies_all_private_routes_and_no_ai(capsys):
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            store, _, _, _, initial = await paired(factory)
            await age_refresh(factory)
            runtime=Runtime()
            owner=PostgresSecurityRuntime(config_factory=lambda:target)
            app=create_app(voice_runtime_factory=lambda:runtime, security_runtime_factory=lambda:owner)
            async with server(app) as port:
                async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=8) as client:
                    payload=dict(session_id=str(initial.session_id), device_id=str(initial.device_id), refresh_id=str(initial.refresh_id))
                    proof={'Authorization':'Refresh '+initial.refresh_credential}
                    renewed=await client.post('/api/security/refresh', json=payload, headers=proof)
                    assert renewed.status_code == 200 and renewed.headers['cache-control']=='no-store'
                    next_token=renewed.json()['access_token']
                    no_private_work(runtime)
                    assert (await client.get('/api/memories', headers={'Authorization':'Bearer '+next_token})).status_code == 200
                    runtime.private_calls.clear()
                    runtime.sessions.sessions.clear() # The preceding authorized read owns this completed session.
                    assert (await client.post('/api/security/refresh', json=payload, headers=proof)).status_code==401
                    for method,path in HTTP:
                        assert (await client.request(method,path,headers={'Authorization':'Bearer '+next_token})).status_code==401
                    for path in WS:
                        with pytest.raises(InvalidStatus):
                            async with connect(f'ws://127.0.0.1:{port}{path}',additional_headers={'Authorization':'Bearer '+next_token}): pytest.fail()
                    no_private_work(runtime)
            assert not owner.ready and owner.session_service is None
            output=capsys.readouterr()
            assert initial.refresh_credential not in output.out+output.err and next_token not in output.out+output.err
    asyncio.run(scenario())


@pytest.mark.parametrize('tamper', ['check','active_index','partial'])
def test_session_schema_tamper_fails_strict_doctor(tamper):
    async def scenario():
        async with owned_store() as (_,_,target,factory,_):
            sql={'check':'ALTER TABLE public.kulai_security_sessions DROP CONSTRAINT ck_session_limits',
                'active_index':'DROP INDEX public.ux_security_refreshes_active',
                'partial':'DROP TABLE public.kulai_security_refreshes'}[tamper]
            async with factory.begin() as session: await session.execute(sa.text(sql))
            assert not (await run_database_doctor(async_url=target.async_url,require_security=True)).ok
    asyncio.run(scenario())


def test_session_audio_authorization_query_budget_and_live_revoke(capsys):
    async def scenario():
        import time
        from backend.tests.test_memory_ws import _start_message, _stop_message
        async with owned_store() as (_,_,target,factory,_):
            store,_,_,_,initial=await paired(factory,actions=frozenset(Action))
            runtime=Runtime(); owner=PostgresSecurityRuntime(config_factory=lambda:target)
            app=create_app(voice_runtime_factory=lambda:runtime,security_runtime_factory=lambda:owner)
            async with server(app) as port:
                statements=[]
                def track(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
                sa.event.listen(owner._engine.sync_engine,'before_cursor_execute',track)
                headers={'Authorization':'Bearer '+initial.access_token}
                try:
                    started=time.perf_counter(); frames=128
                    async with connect(f'ws://127.0.0.1:{port}/ws/memory',additional_headers=headers) as ws:
                        await ws.send(json.dumps(_start_message(uuid4()))); await ws.recv()
                        for _ in range(frames): await ws.send(b'\0\0'*512)
                        await ws.send(json.dumps(_stop_message()))
                        events=[json.loads(await ws.recv()) for _ in range(3)]
                        assert events[-1]['type']=='memory.saved'
                    lookups=sum('LEFT OUTER JOIN public.kulai_security_refreshes' in s for s in statements)
                    assert lookups==frames+3 and len(statements)==2*lookups
                    with capsys.disabled(): print(f'sessions.audio.frames={frames};lookups={lookups};sql={len(statements)};elapsed_ms={(time.perf_counter()-started)*1000:.2f};pool_checkout={owner._engine.pool.checkedout()}')
                    async with connect(f'ws://127.0.0.1:{port}/ws/memory',additional_headers=headers) as ws:
                        await ws.send(json.dumps(_start_message(uuid4()))); await ws.recv()
                        before=len(runtime.private_calls)
                        await CredentialManager(store).revoke_device(initial.device_id)
                        await ws.send(b'\0\0'*512)
                        from websockets.exceptions import ConnectionClosed
                        with pytest.raises(ConnectionClosed) as closed: await ws.recv()
                        assert closed.value.rcvd.code==1008 and len(runtime.private_calls)==before
                    assert owner._engine.pool.checkedout()==0
                finally: sa.event.remove(owner._engine.sync_engine,'before_cursor_execute',track)
    asyncio.run(scenario())
