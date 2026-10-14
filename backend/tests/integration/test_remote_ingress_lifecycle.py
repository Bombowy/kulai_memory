"""Additional actual TLS version, pin, cancellation and bounded lifetime proofs."""
import asyncio
import json
import ssl

import httpx
import pytest
import sqlalchemy as sa
from websockets.exceptions import ConnectionClosed

from backend.tests.remote_tls_fixtures import certificates
from backend.tests.integration.test_remote_ingress_tls import pipeline, client, websocket, headers
from backend.tests.integration.test_sessions_postgres import age_refresh
from backend.tests.test_memory_ws import _start_message, _stop_message
from backend.tests.test_security_transport import no_private_work
from uuid import uuid4


@pytest.mark.parametrize('version',[ssl.TLSVersion.TLSv1_2,ssl.TLSVersion.TLSv1_3])
def test_actual_tls_versions_have_verified_gateway_and_bearer(certificates,version):
    async def run():
        async with pipeline(certificates) as (port,_,_,_,_,credential):
            context = certificates.client()
            context.minimum_version = context.maximum_version = version
            async with httpx.AsyncClient(base_url=f'https://127.0.0.1:{port}',verify=context,
                    trust_env=False,headers={'Host':'memory.test'},timeout=5) as transport:
                response = await transport.get('/api/memories',headers=headers(credential))
                assert response.status_code == 200
                assert response.extensions['network_stream'].get_extra_info('ssl_object').version() == (
                    'TLSv1.2' if version == ssl.TLSVersion.TLSv1_2 else 'TLSv1.3')
    asyncio.run(run())


def test_valid_ca_and_san_with_wrong_leaf_pin_is_denied(certificates):
    async def run():
        async with pipeline(certificates,gateway_sha256='0'*64) as (port,_,runtime,_,_,credential):
            async with client(certificates,port) as transport:
                with pytest.raises(httpx.TransportError):
                    await transport.get('/api/memories',headers=headers(credential))
            no_private_work(runtime)
    asyncio.run(run())


def test_correct_leaf_pin_and_explicit_public_port_are_accepted(certificates):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    pin = x509.load_pem_x509_certificate(certificates.path('gateway','pem').read_bytes()).fingerprint(hashes.SHA256()).hex()
    async def run():
        async with pipeline(certificates,gateway_sha256=pin,public_origin='https://memory.test:9443') as (port,_,_,_,_,credential):
            async with client(certificates,port) as transport:
                outgoing = {**headers(credential),'Host':'memory.test:9443'}
                assert (await transport.get('/api/memories',headers=outgoing)).status_code == 200
                assert (await transport.get('/api/memories',headers=headers(credential))).status_code == 403
    asyncio.run(run())


@pytest.mark.parametrize('attack',['absolute-http','duplicate-host','conflicting-length'])
def test_real_tls_raw_http_target_and_conflicting_headers_fail_closed(certificates,attack):
    async def run():
        async with pipeline(certificates) as (port,_,runtime,_,_,credential):
            reader,writer = await asyncio.open_connection('127.0.0.1',port,ssl=certificates.client(),server_hostname='127.0.0.1')
            target = 'http://memory.test/api/memories' if attack == 'absolute-http' else '/api/memories'
            extra = 'Host: evil.test\r\n' if attack == 'duplicate-host' else (
                'Content-Length: 1\r\nContent-Length: 2\r\n' if attack == 'conflicting-length' else '')
            writer.write((f'GET {target} HTTP/1.1\r\nHost: memory.test\r\nAuthorization: Bearer {credential.access_token}\r\n'
                          +extra+'Connection: close\r\n\r\n').encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(),5)
            assert response.split(b'\r\n',1)[0].split()[1] in (b'400',b'403',b'404')
            writer.close()
            await writer.wait_closed()
            no_private_work(runtime)
    asyncio.run(run())


def test_http_total_timeout_cancels_inference_without_unsafe_not_applied_claim(certificates):
    async def run():
        async with pipeline(certificates,http_timeout=.2) as (port,_,runtime,_,_,credential):
            entered,drained = asyncio.Event(),asyncio.Event()
            async def pending(**kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
            runtime.ask_memory = pending
            async with client(certificates,port) as transport:
                result = await transport.post('/api/assistant/ask',headers=headers(credential),json={'query':'Synthetic timeout'})
                assert result.status_code == 503 and 'request_applied' not in result.json()
            assert entered.is_set()
            await asyncio.wait_for(drained.wait(),2)
    asyncio.run(run())


def test_wss_idle_timeout_closes_and_cleans_audio(certificates):
    async def run():
        async with pipeline(certificates,idle_timeout=.3) as (port,_,runtime,_,_,credential):
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                with pytest.raises(ConnectionClosed) as denial:
                    await ws.recv()
                assert denial.value.rcvd.code == 1013
            assert runtime.ingest_calls == []
    asyncio.run(run())


def test_disconnect_after_refresh_commit_never_retries_or_duplicates_rotation(certificates):
    async def run():
        async with pipeline(certificates) as (port,app,_,factory,_,credential):
            await age_refresh(factory)
            store = app.state.security_runtime.session_service._store
            original = store.renew
            committed,drained = asyncio.Event(),asyncio.Event()
            calls = []
            async def delayed(*args):
                calls.append(True)
                result = await original(*args)
                assert result.credential is not None
                committed.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
            store.renew = delayed
            body = json.dumps({'session_id':str(credential.session_id),'device_id':str(credential.device_id),'refresh_id':str(credential.refresh_id)}).encode()
            _,writer = await asyncio.open_connection('127.0.0.1',port,ssl=certificates.client(),server_hostname='127.0.0.1')
            request = (f'POST /api/security/refresh HTTP/1.1\r\nHost: memory.test\r\nAuthorization: Refresh {credential.refresh_credential}\r\n'
                       f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body
            writer.write(request)
            await writer.drain()
            await asyncio.wait_for(committed.wait(),5)
            writer.transport.abort()
            await asyncio.wait_for(drained.wait(),5)
            assert len(calls) == 1
            async with factory() as session:
                assert await session.scalar(sa.text('SELECT generation FROM kulai_security_sessions')) == 1
                assert await session.scalar(sa.text('SELECT count(*) FROM kulai_security_refreshes')) == 2
            async with client(certificates,port) as transport:
                assert (await transport.get('/api/memories',headers=headers(credential))).status_code == 401
    asyncio.run(run())
