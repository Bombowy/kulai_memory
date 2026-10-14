"""Real HTTPS/WSS -> real Nginx mTLS -> opaque TCP -> ASGI -> owned PG."""
import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import ssl
from uuid import UUID,uuid4

import httpx
import pytest
import sqlalchemy as sa
from websockets.exceptions import ConnectionClosed,InvalidStatus

from backend.tests.nginx_fixtures import gateway_server,bounded_test
from backend.tests.remote_tls_fixtures import certificates
from backend.tests.integration.test_remote_ingress_tls import application,headers,PgRuntime
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_sessions_postgres import paired,age_refresh
from backend.tests.test_memory_ws import _start_message,_stop_message
from backend.tests.test_server_transport import start,stop
from backend.tests.test_security_transport import no_private_work
from kulai_memory.security import Action,CredentialManager
from kulai_memory.security.authentication import utc_now
from kulai_memory.pairing.domain import PairingService
from kulai_memory.security_persistence.sessions import PostgresSessionStore


@asynccontextmanager
async def pipeline(certificates,**changes):
    async with owned_store() as (_,_,target,factory,_):
        store,_,_,_,credential=await paired(factory,actions=frozenset(Action))
        app,runtime=application(target,factory,certificates.config(**changes))
        async with gateway_server(app,certificates) as gateway:
            yield gateway,app,runtime,factory,store,credential


def test_real_nginx_https_wss_authorization_note_ask_and_no_double_memory(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,factory,_,credential):
            async with g.client() as client:
                assert (await client.get('/api/memories')).status_code==401
                response=await client.get('/api/memories',headers=headers(credential))
                assert response.status_code==200 and response.json()['items']==[]
                assert (await client.get('/api/memories',headers=headers(credential),params={'archived':'false','limit':20})).status_code==200
                assert response.headers['cache-control']=='no-store'
                assert (await client.post('/api/assistant/ask',headers=headers(credential),json={'query':'Synthetic gateway query'})).status_code==200
                assert (await client.post('/api/assistant/speech-plan',headers=headers(credential),json={'answer':'Synthetic answer'})).status_code==200
            ingestion=uuid4()
            for path in ('/ws/memory','/ws/assistant'):
                with pytest.raises(InvalidStatus) as denial:
                    async with g.websocket(path):
                        pytest.fail('Anonymous WSS')
                assert denial.value.response.status_code==403
                async with g.websocket(path,credential) as ws:
                    await ws.send(json.dumps(_start_message(ingestion) if path.endswith('memory') else start()))
                    assert json.loads(await ws.recv())['type']=='session.ready'
                    await ws.send(b'\0\0'*1600)
                    await ws.send(json.dumps(_stop_message() if path.endswith('memory') else stop()))
                    events=[json.loads(await ws.recv()) for _ in range(3)]
                    assert events[-1]['type']==('memory.saved' if path.endswith('memory') else 'assistant.completed')
            async with g.client() as client:
                page=(await client.get('/api/memories',headers=headers(credential))).json()
                assert len(page['items'])==1
                assert (await client.get('/api/memories/'+page['items'][0]['id'],headers=headers(credential))).status_code==200
            assert len(runtime.ingest_calls)==1
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==1
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('identity',['foreign','wrong-san','expired','wrong-purpose'])
def test_nginx_wrong_upstream_client_certificate_denied(certificates,identity):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            await g.stop()
            await g.start(gateway_certificate=str(certificates.path(identity,'pem')),gateway_private_key=str(certificates.path(identity,'key')))
            async with g.client() as client:
                result=await client.get('/api/memories',headers=headers(credential))
                assert result.status_code==503 and result.json()=={'detail':'Remote access unavailable.'}
            no_private_work(runtime)
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('name',['wrong-server','wrong-server-name'])
def test_nginx_verifies_ingress_ca_and_san(certificates,name):
    async def run():
        async with pipeline(certificates,certificate=certificates.path(name,'pem'),private_key=certificates.path(name,'key')) as (g,_,runtime,_,_,credential):
            async with g.client() as client:
                assert (await client.get('/api/memories',headers=headers(credential))).status_code==503
            no_private_work(runtime)
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('host',['evil.test','memory.test:0','memory.test:444','memory.test:','memory.test?','memory.test#'])
def test_nginx_public_host_rejected_before_backend(certificates,host):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            async with g.client() as client:
                assert (await client.get('/api/memories',headers={**headers(credential),'Host':host})).status_code in (400,421)
            no_private_work(runtime)
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('header',['Forwarded','X-Forwarded-For','X-Forwarded-Proto','X-Verified-Client','X-Verified-Arbitrary','X-Real-IP','X-Kulai-Ingress-Identity'])
def test_nginx_header_allowlist_does_not_create_authentication(certificates,header):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            async with g.client() as client:
                assert (await client.get('/api/memories',headers={header:'gateway.test'})).status_code==401
                # A valid bearer stays valid, but the arbitrary assertion never reaches ASGI.
                assert (await client.get('/api/memories',headers={**headers(credential),header:'gateway.test'})).status_code==200
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('path',['/health','/docs','/openapi.json','/admin','/api/pairing/approve','/ollama/api/generate','/postgres','/api/%6demories','/api/memories/','/ws/memory?access_token=synthetic'])
def test_nginx_exact_routes_deny_admin_other_services_and_normalization(certificates,path):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            async with g.client() as client:
                assert (await client.get(path,headers=headers(credential))).status_code==404
            no_private_work(runtime)
    asyncio.run(bounded_test(run()))


def test_nginx_pairing_refresh_once_replay_and_redacted_logs(certificates,caplog):
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            store=PostgresSessionStore(factory)
            invite=await PairingService(store,clock=utc_now).invite(actions=frozenset(Action))
            receiver=secrets.token_urlsafe(32)
            app,_=application(target,factory,certificates.config())
            async with gateway_server(app,certificates) as g:
                async with g.client() as client:
                    request=await client.post('/api/pairing/request',headers={'Authorization':'Pairing '+invite.secret},
                        json={'session_id':str(invite.session_id),'receiver_secret':receiver,'client_id':str(uuid4()),'device_label':'Synthetic gateway device'})
                    assert request.status_code==200
                    device=UUID(request.json()['device_id'])
                    proof={'Authorization':'Pairing '+receiver}
                    body={'session_id':str(invite.session_id)}
                    assert (await client.post('/api/pairing/claim',headers=proof,json=body)).status_code==400
                    await store.decide(invite.session_id,device,approve=True)
                    assert (await client.post('/api/pairing/status',headers=proof,json=body)).json()['state']=='approved'
                    result=await client.post('/api/pairing/claim',headers=proof,json=body)
                    assert result.status_code==200 and UUID(result.json()['device_id'])==device
                    value=result.json()
                    await age_refresh(factory)
                    renew={'Authorization':'Refresh '+value['refresh_credential']}
                    body={key:value[key] for key in ('session_id','device_id','refresh_id')}
                    rotated=await client.post('/api/security/refresh',headers=renew,json=body)
                    assert rotated.status_code==200
                    newer=rotated.json()
                    # Do not reuse the rotated bearer; a predecessor is immediately denied.
                    assert (await client.get('/api/memories',headers={'Authorization':'Bearer '+newer['access_token']})).status_code==200
                    replay=await client.post('/api/security/refresh',headers=renew,json=body)
                    assert replay.status_code==401
                    assert (await client.get('/api/memories',headers={'Authorization':'Bearer '+newer['access_token']})).status_code==401
                log=(g.directory/'safe.log').read_text(encoding='utf-8')+caplog.text
                for secret in (invite.secret,receiver,value['refresh_credential'],value['access_token'],newer['access_token']):
                    assert secret not in log
                assert '/api/' not in log and 'Synthetic gateway device' not in log
                async with factory() as db:
                    assert await db.scalar(sa.text('SELECT generation FROM kulai_security_sessions'))==1
    asyncio.run(bounded_test(run()))


def test_nginx_revocation_closes_active_websocket(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,store,credential):
            async with g.websocket('/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                await CredentialManager(store).revoke_device(credential.device_id)
                await ws.send(b'\0\0'*10)
                with pytest.raises(ConnectionClosed) as denial:
                    await ws.recv()
                assert denial.value.rcvd.code==1008
            assert runtime.ingest_calls==[]
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('path',['/ws/memory','/ws/assistant'])
def test_nginx_disconnect_cancels_processing_and_cleans_audio(certificates,path):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            entered,drained=asyncio.Event(),asyncio.Event()
            audio=[]
            async def pending(**kwargs):
                audio.append(kwargs['audio_path'])
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
            setattr(runtime,'transcribe' if path.endswith('memory') else 'transcribe_question',pending)
            async with g.websocket(path,credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4()) if path.endswith('memory') else start()))
                await ws.recv()
                await ws.send(b'\0\0'*1600)
                await ws.send(json.dumps(_stop_message() if path.endswith('memory') else stop()))
                await asyncio.wait_for(entered.wait(),5)
                ws.transport.abort()
            await asyncio.wait_for(drained.wait(),5)
            for _ in range(100):
                if all(not p.exists() for p in audio): break
                await asyncio.sleep(.02)
            assert all(not p.exists() for p in audio) and not runtime.ingest_calls
    asyncio.run(bounded_test(run()))


def test_nginx_large_pcm_and_malformed_frames_and_http_limits(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            async with g.websocket('/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                for _ in range(80):
                    await ws.send(b'\0'*240000)
                await ws.send(json.dumps(_stop_message()))
                events=[json.loads(await ws.recv()) for _ in range(3)]
                assert events[-1]['type']=='memory.saved'
            async with g.websocket('/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                await ws.send(b'x'*262146)
                with pytest.raises(ConnectionClosed) as denial: await ws.recv()
                # A rejecting upstream may reset while Nginx is still writing
                # the oversized frame. No close frame (1006) is then observable.
                # Both paths MUST disconnect and MUST NOT create another Memory.
                assert denial.value.rcvd is None or denial.value.rcvd.code==1009
            async with g.websocket('/ws/assistant',credential) as ws:
                await ws.send('{invalid')
                error=json.loads(await ws.recv())
                assert error['type']=='error' and error['payload']['code']=='protocol.invalid_message'
            async with g.client() as client:
                assert (await client.post('/api/assistant/ask',headers=headers(credential),content=b'x'*65537)).status_code==413
            assert len(runtime.ingest_calls)==1
    asyncio.run(bounded_test(run()))


def test_nginx_and_bridge_restart_unavailable_backend_no_queue(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            await g.bridge.close()
            async with g.client() as client:
                result=await client.get('/api/memories',headers=headers(credential))
                assert result.status_code==503 and 'request_applied' not in result.json()
            await g.bridge.start()
            await g.stop()
            await g.start()
            assert g.generation==2
            async with g.client() as client:
                assert (await client.get('/api/memories',headers=headers(credential))).status_code==200
            g.ingress.should_exit=True
            async with g.client() as client:
                assert (await client.get('/api/memories',headers=headers(credential))).status_code==503
    asyncio.run(bounded_test(run()))


def test_nginx_refresh_disconnect_after_commit_is_not_retried(certificates):
    async def run():
        async with pipeline(certificates) as (g,app,_,factory,_,credential):
            await age_refresh(factory)
            store=app.state.security_runtime.session_service._store
            original=store.renew
            committed,drained=asyncio.Event(),asyncio.Event()
            calls=[]
            async def delayed(*args):
                calls.append(True)
                result=await original(*args)
                assert result.credential is not None
                committed.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
            store.renew=delayed
            async with g.client() as transport:
                task=asyncio.create_task(transport.post('/api/security/refresh',
                    headers={'Authorization':'Refresh '+credential.refresh_credential},
                    json={key:str(getattr(credential,key)) for key in ('session_id','device_id','refresh_id')}))
                await asyncio.wait_for(committed.wait(),5)
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            await asyncio.wait_for(drained.wait(),5)
            assert len(calls)==1
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT generation FROM kulai_security_sessions'))==1
                assert await db.scalar(sa.text('SELECT count(*) FROM kulai_security_refreshes'))==2
    asyncio.run(bounded_test(run()))


def test_nginx_browser_origin_duplicate_host_and_wrong_sni_denied(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            async with g.client() as transport:
                assert (await transport.get('/api/memories',headers={**headers(credential),'Origin':'https://memory.test'})).status_code==403
            reader,writer=await asyncio.open_connection('127.0.0.1',g.port,ssl=certificates.client(None),server_hostname='memory.test')
            writer.write(b'GET /api/memories HTTP/1.1\r\nHost: memory.test\r\nHost: evil.test\r\nConnection: close\r\n\r\n')
            await writer.drain()
            response=await asyncio.wait_for(reader.read(4096),5)
            assert response.split(b'\r\n',1)[0].split()[1]==b'400'
            writer.close()
            await writer.wait_closed()
            with pytest.raises((OSError,ssl.SSLError)):
                await asyncio.open_connection('127.0.0.1',g.port,ssl=certificates.client(None),server_hostname='evil.test')
            no_private_work(runtime)
    asyncio.run(bounded_test(run()))


def test_nginx_bridge_loss_while_recording_has_no_write_and_fresh_session_works(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,factory,_,credential):
            async with g.websocket('/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                await ws.send(b'\0\0'*1600)
                await g.bridge.close()
                with pytest.raises(ConnectionClosed): await ws.recv()
            await g.bridge.start()
            async with g.client() as transport:
                assert (await transport.get('/api/memories',headers=headers(credential))).json()['items']==[]
            assert runtime.ingest_calls==[]
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==0
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('version',[ssl.TLSVersion.TLSv1_2,ssl.TLSVersion.TLSv1_3])
def test_public_nginx_real_tls_versions(certificates,version):
    async def run():
        async with pipeline(certificates) as (g,_,_,_,_,credential):
            async with g.client(version) as transport:
                response=await transport.get('/api/memories',headers=headers(credential))
                assert response.status_code==200
                assert response.extensions['network_stream'].get_extra_info('ssl_object').version()==(
                    'TLSv1.2' if version==ssl.TLSVersion.TLSv1_2 else 'TLSv1.3')
    asyncio.run(bounded_test(run()))


def test_nginx_real_connection_and_request_rate_limits(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            sockets=[]
            try:
                for _ in range(4): sockets.append(await g.websocket('/ws/memory',credential))
                async with g.client() as transport:
                    assert (await transport.get('/api/memories',headers=headers(credential))).status_code==429
            finally:
                await asyncio.gather(*(ws.close() for ws in sockets))
            async with g.client() as transport:
                # A real burst, independent of backend checkpoint latency.
                responses=await asyncio.gather(*(transport.get('/api/memories') for _ in range(60)))
                statuses=[response.status_code for response in responses]
            assert 429 in statuses and set(statuses)<={401,429}
            await g.stop()
            log=(g.directory/'safe.log').read_text(encoding='utf-8')
            assert 'req=REJECTED' in log and 'conn=REJECTED' in log
            assert runtime.ingest_calls==[]
    asyncio.run(bounded_test(run()))
