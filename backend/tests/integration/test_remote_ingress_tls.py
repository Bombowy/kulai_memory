"""Actual mTLS HTTPS/WSS + managed security v3 + owned PostgreSQL, synthetic AI."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging
import secrets
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from backend.tests.remote_tls_fixtures import certificates, tls_server
from backend.tests.security_postgres_fixtures import owned_store, CURRENT_OWNED
from backend.tests.integration.test_sessions_postgres import paired, age_refresh
from backend.tests.test_memory_ws import _start_message, _stop_message
from backend.tests.test_server_transport import start, stop
from backend.tests.test_security_transport import Runtime, no_private_work
from kulai_memory.app_factory import create_app
from kulai_memory.application import MemoryService, TranscriptMemoryIngestionService
from kulai_memory.history_persistence import read_memory_history, read_memory_detail
from kulai_memory.pairing.domain import PairingService
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.remote_ingress import RemoteIngressError
from kulai_memory.security import Action, CredentialManager, SecurityConfig, SecurityMode
from kulai_memory.security.authentication import utc_now
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime
from kulai_memory.security_persistence.sessions import PostgresSessionStore


class PgRuntime(Runtime):
    """Reuse the real canonical repository; only inference providers are synthetic."""
    def __init__(self, factory):
        self.factory = factory
        super().__init__()
    async def list_memories(self, **kwargs):
        return await read_memory_history(session_factory=self.factory, **kwargs)
    async def get_memory(self, **kwargs):
        return await read_memory_detail(session_factory=self.factory, **kwargs)
    async def ingest(self, **kwargs):
        self.ingest_calls.append((kwargs['ingestion_id'], kwargs['session_id'], kwargs['transcription'].text))
        async with self.factory.begin() as session:
            service = TranscriptMemoryIngestionService(memory_service=MemoryService(
                repository=PostgresMemoryRepository(db=session)))
            return await service.ingest(**kwargs)


def application(target, factory, config, runtime=None):
    runtime = runtime or PgRuntime(factory)
    app = create_app(remote_ingress_config=config, voice_runtime_factory=lambda: runtime,
        security_runtime_factory=lambda: PostgresSecurityRuntime(config_factory=lambda: target))
    fixture = CURRENT_OWNED.get()
    if fixture is not None and fixture[2].name == target.name:
        app.state._owned_gate_fixture = fixture
    return app, runtime


@asynccontextmanager
async def pipeline(certificates, **changes):
    async with owned_store() as (_, _, target, factory, _):
        store, _, _, _, credential = await paired(factory, actions=frozenset(Action))
        app, runtime = application(target, factory, certificates.config(**changes))
        async with tls_server(app) as (port, host):
            yield port, app, runtime, factory, store, credential


def client(certificates, port, identity='gateway', **kwargs):
    # httpx's INFO URL diagnostics are client-driver logs, not ingress logs.
    # The hostile query-secret test must not leak its sentinel in the driver.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    return httpx.AsyncClient(base_url=f'https://127.0.0.1:{port}',
        verify=certificates.client(identity), headers={'Host': 'memory.test'},
        follow_redirects=False, trust_env=False, timeout=10, **kwargs)


def headers(credential):
    return {'Authorization': 'Bearer '+credential.access_token, 'X-Kulai-Device-ID': str(credential.device_id)}


def websocket(certificates, port, path, credential=None, **kwargs):
    return connect('wss://memory.test'+path, host='127.0.0.1', port=port,
        server_hostname='127.0.0.1', ssl=certificates.client(), proxy=None,
        additional_headers=headers(credential) if credential else {}, open_timeout=10,
        close_timeout=3, **kwargs)


@pytest.mark.parametrize('identity', [None, 'foreign', 'wrong-san', 'expired', 'wrong-purpose'])
def test_real_mtls_rejects_missing_foreign_expired_wrong_san_and_eku(certificates, identity):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, _, _, credential):
            async with client(certificates, port, identity) as transport:
                with pytest.raises(httpx.TransportError):
                    await transport.get('/api/memories', headers=headers(credential))
            no_private_work(runtime)
    asyncio.run(run())


@pytest.mark.parametrize('name', ['wrong-server', 'wrong-server-name'])
def test_real_tls_client_verifies_server_chain_and_hostname(certificates, name):
    async def run():
        async with pipeline(certificates, certificate=certificates.path(name,'pem'),
                            private_key=certificates.path(name,'key')) as (port, _, runtime, _, _, credential):
            async with client(certificates, port) as transport:
                with pytest.raises(httpx.ConnectError):
                    await transport.get('/api/memories', headers=headers(credential))
            no_private_work(runtime)
    asyncio.run(run())


def test_https_and_wss_authentication_real_peer_and_scope_authorization(certificates):
    async def run():
        async with pipeline(certificates) as (port, app, runtime, factory, store, credential):
            async with client(certificates, port) as transport:
                assert (await transport.get('/api/memories')).status_code == 401
                assert (await transport.get('/api/memories', headers=headers(credential))).json()['items'] == []
                assert (await transport.post('/api/assistant/ask', headers=headers(credential), json={'query':'Synthetic'})).status_code == 200
                assert (await transport.post('/api/assistant/speech-plan', headers=headers(credential), json={'answer':'Synthetic answer'})).status_code == 200
            for path in ('/ws/memory','/ws/assistant'):
                with pytest.raises(InvalidStatus) as denial:
                    async with websocket(certificates, port, path):
                        pytest.fail('Anonymous WSS accepted')
                assert denial.value.response.status_code == 403
                async with websocket(certificates, port, path, credential) as ws:
                    await ws.send(json.dumps(_start_message(uuid4()) if path.endswith('memory') else start()))
                    assert json.loads(await ws.recv())['type'] == 'session.ready'
                    await ws.send(b'\0\0'*1600)
                    await ws.send(json.dumps(_stop_message() if path.endswith('memory') else stop()))
                    events = [json.loads(await ws.recv()) for _ in range(3)]
                    assert events[-1]['type'] == ('memory.saved' if path.endswith('memory') else 'assistant.completed')
            async with client(certificates, port) as transport:
                page = (await transport.get('/api/memories',headers=headers(credential))).json()
                assert len(page['items']) == 1
                detail = await transport.get('/api/memories/'+page['items'][0]['id'],headers=headers(credential))
                assert detail.status_code == 200
            limited = await CredentialManager(store).issue(credential.device_id,actions=frozenset({Action.MEMORY_READ}))
            async with client(certificates, port) as transport:
                assert (await transport.post('/api/assistant/ask',headers=headers(limited),json={'query':'Denied'})).status_code == 403
    asyncio.run(run())


@pytest.mark.parametrize('attack', ['verified','forwarded','forwarded-for','real-ip','origin','host','port','zero-port','empty-port','empty-query','empty-fragment','duplicate-auth','query-secret','encoded-path','duplicate-origin'])
def test_real_https_spoofed_headers_and_public_origin_deny_before_private_work(certificates, attack):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, _, _, credential):
            outgoing = headers(credential)
            path = '/api/memories'
            values = {'verified': ('X-Verified-Client','gateway.test'),
                'forwarded': ('Forwarded','for=127.0.0.1;proto=https'),
                'forwarded-for': ('X-Forwarded-For','127.0.0.1'), 'real-ip': ('X-Real-IP','127.0.0.1'),
                'origin': ('Origin','https://memory.test'), 'host': ('Host','other.test'),
                'port': ('Host','memory.test:444'),'zero-port': ('Host','memory.test:0'),
                'empty-port': ('Host','memory.test:'),'empty-query': ('Host','memory.test?'),
                'empty-fragment': ('Host','memory.test#')}
            if attack in values:
                outgoing[values[attack][0]] = values[attack][1]
            if attack == 'duplicate-auth':
                outgoing = [('Authorization',outgoing['Authorization']),('authorization',outgoing['Authorization'])]
            if attack == 'duplicate-origin':
                outgoing = [*outgoing.items(),('Origin',''),('origin','https://evil.test')]
            if attack == 'query-secret':
                path += '?access_token='+credential.access_token
            if attack == 'encoded-path':
                path = '/api/%6demories'
            async with client(certificates, port) as transport:
                assert (await transport.get(path,headers=outgoing)).status_code in (400,401,403)
            no_private_work(runtime)
    asyncio.run(run())


@pytest.mark.parametrize('path', ['/docs','/openapi.json','/health','/api/security/admin','/ollama/api/generate','/postgres','/api/pairing/approve','/api/memories/'])
def test_real_https_unknown_and_admin_routes_are_not_exposed(certificates,path):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, _, _, credential):
            async with client(certificates,port) as transport:
                assert (await transport.get(path,headers=headers(credential))).status_code == 404
            no_private_work(runtime)
    asyncio.run(run())


def test_pairing_exact_approval_refresh_single_rotation_and_replay_revocation_over_https(certificates,caplog):
    async def run():
        async with owned_store() as (_, _, target, factory, _):
            store = PostgresSessionStore(factory)
            pairing = PairingService(store,clock=utc_now)
            invitation = await pairing.invite(actions=frozenset(Action))
            receiver = secrets.token_urlsafe(32)
            app, runtime = application(target,factory,certificates.config())
            async with tls_server(app) as (port,_):
                async with client(certificates,port) as transport:
                    result = await transport.post('/api/pairing/request',headers={'Authorization':'Pairing '+invitation.secret},
                        json={'session_id':str(invitation.session_id),'receiver_secret':receiver,'client_id':str(uuid4()),'device_label':'Owned remote test'})
                    assert result.status_code == 200
                    device = result.json()['device_id']
                    proof = {'Authorization':'Pairing '+receiver}
                    body = {'session_id':str(invitation.session_id)}
                    assert (await transport.post('/api/pairing/claim',headers=proof,json=body)).status_code == 400
                    from uuid import UUID
                    with pytest.raises(Exception):
                        await store.decide(invitation.session_id,uuid4(),approve=True)
                    await store.decide(invitation.session_id,UUID(device),approve=True)
                    status = await transport.post('/api/pairing/status',headers=proof,json=body)
                    assert status.json()['state'] == 'approved' and status.json()['device_id'] == device
                    claimed = await transport.post('/api/pairing/claim',headers=proof,json=body)
                    assert claimed.status_code == 200 and claimed.json()['device_id'] == device
                    value = claimed.json()
                    assert (await transport.post('/api/pairing/claim',headers=proof,json=body)).status_code == 400
                    await age_refresh(factory)
                    renew_headers = {'Authorization':'Refresh '+value['refresh_credential']}
                    renew_body = {name:value[name] for name in ('session_id','device_id','refresh_id')}
                    next_response = await transport.post('/api/security/refresh',headers=renew_headers,json=renew_body)
                    assert next_response.status_code == 200
                    next_value = next_response.json()
                    assert next_value['token_id'] != value['token_id']
                    assert next_value['refresh_id'] != value['refresh_id']
                    assert (await transport.get('/api/memories',headers={'Authorization':'Bearer '+next_value['access_token']})).status_code == 200
                    assert (await transport.post('/api/security/refresh',headers=renew_headers,json=renew_body)).status_code == 401
                    assert (await transport.get('/api/memories',headers={'Authorization':'Bearer '+next_value['access_token']})).status_code == 401
                    async with factory() as session:
                        assert await session.scalar(sa.text('SELECT generation FROM kulai_security_sessions')) == 1
                    for secret in (invitation.secret,receiver,value['access_token'],value['refresh_credential'],next_value['access_token']):
                        assert secret not in caplog.text
    asyncio.run(run())


def test_revoked_access_and_device_fail_https_and_existing_wss(certificates):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, _, store, credential):
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                await CredentialManager(store).revoke_device(credential.device_id)
                await ws.send(b'\0\0'*10)
                with pytest.raises(ConnectionClosed) as denial:
                    await ws.recv()
                assert denial.value.rcvd.code == 1008
            async with client(certificates,port) as transport:
                assert (await transport.get('/api/memories',headers=headers(credential))).status_code == 401
            assert runtime.ingest_calls == []
    asyncio.run(run())


@pytest.mark.parametrize('path', ['/ws/memory','/ws/assistant'])
def test_wss_disconnect_during_processing_cancels_and_cleans_owned_audio(certificates,path):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, _, _, credential):
            entered, drained = asyncio.Event(), asyncio.Event()
            audio = []
            async def pending(**kwargs):
                audio.append(kwargs['audio_path'])
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
            setattr(runtime,'transcribe' if path.endswith('memory') else 'transcribe_question',pending)
            async with websocket(certificates,port,path,credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4()) if path.endswith('memory') else start()))
                await ws.recv()
                await ws.send(b'\0\0'*1600)
                await ws.send(json.dumps(_stop_message() if path.endswith('memory') else stop()))
                await asyncio.wait_for(entered.wait(),5)
                ws.transport.abort()
            await asyncio.wait_for(drained.wait(),5)
            for _ in range(100):
                if all(not file.exists() for file in audio):
                    break
                await asyncio.sleep(.01)
            assert all(not file.exists() for file in audio)
            assert runtime.ingest_calls == []
    asyncio.run(run())


def test_pcm_volume_frame_and_body_limits(certificates):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, factory, _, credential):
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                for _ in range(80):
                    await ws.send(b'\0'*(240000))  # 19.2 MB = ten minutes PCM, accelerated volume test.
                await ws.send(json.dumps(_stop_message()))
                events = [json.loads(await ws.recv()) for _ in range(3)]
                assert events[-1]['type'] == 'memory.saved'
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                await ws.send(b'\0'*262146)
                with pytest.raises(ConnectionClosed) as denial:
                    await ws.recv()
                assert denial.value.rcvd.code == 1009
            async with client(certificates,port) as transport:
                assert (await transport.post('/api/assistant/ask',headers=headers(credential),content=b'x'*65537)).status_code == 413
            async with factory() as session:
                assert await session.scalar(sa.text('SELECT count(*) FROM memories')) == 1
    asyncio.run(run())


def test_restart_revalidates_schema_and_cannot_start_local_dev_or_missing_schema(certificates):
    async def run():
        with pytest.raises(RemoteIngressError):
            create_app(security_config=SecurityConfig(mode=SecurityMode.LOCAL_DEV),remote_ingress_config=certificates.config())
        async with owned_store(security=False) as (_, _, target, factory, _):
            app, runtime = application(target,factory,certificates.config())
            with pytest.raises(RemoteIngressError):
                async with tls_server(app):
                    pytest.fail('Missing security schema opened listener')
            assert runtime.startup_count == 0
        async with owned_store() as (_, _, target, factory, _):
            _, _, _, _, credential = await paired(factory,actions=frozenset(Action))
            for _ in range(2):
                app, runtime = application(target,factory,certificates.config())
                async with tls_server(app) as (port,_):
                    async with client(certificates,port) as transport:
                        assert (await transport.get('/api/memories',headers=headers(credential))).status_code == 200
                assert runtime.startup_count == runtime.shutdown_count == 1
    asyncio.run(run())


@pytest.mark.parametrize('table,revision', [('alembic_version','kulai_memory_0003'),('kulai_security_version','kulai_security_0002')])
def test_schema_tamper_fails_closed_until_new_backend_lifespan(certificates,table,revision):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, factory, _, credential):
            async with factory.begin() as session:
                await session.execute(sa.text('UPDATE '+table+' SET version_num=:revision'),{'revision':revision})
            async with client(certificates,port) as transport:
                assert (await transport.get('/api/memories',headers=headers(credential))).status_code == 503
                async with factory.begin() as session:
                    await session.execute(sa.text('UPDATE '+table+' SET version_num=:revision'),
                        {'revision':'kulai_memory_0004' if table=='alembic_version' else 'kulai_security_0003'})
                assert (await transport.get('/api/memories',headers=headers(credential))).status_code == 503
            no_private_work(runtime)
    asyncio.run(run())


def test_private_inputs_and_credentials_never_appear_in_protocol_or_application_logs(certificates,caplog,capsys):
    async def run():
        async with pipeline(certificates) as (port, _, runtime, _, _, credential):
            private = 'PRIVATE_REMOTE_NOTE_4D3B1'
            async with client(certificates,port) as transport:
                await transport.get('/api/memories?access_token='+credential.access_token,headers={'User-Agent':private})
                await transport.post('/api/assistant/ask',headers=headers(credential),json={'query':private})
            output = capsys.readouterr()
            combined = caplog.text+output.out+output.err
            assert credential.access_token not in combined and credential.refresh_credential not in combined
            assert private not in combined
    asyncio.run(run())
