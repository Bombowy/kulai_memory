"""Real loopback Nginx + existing mTLS ingress/bridge; in-memory security only.

No PostgreSQL, migrations, production keys, WireGuard changes or external sockets.
The host's three existing locations are synthetic sentinels, not live demo sites.
"""
import asyncio
from contextlib import asynccontextmanager
import os
import logging
from pathlib import Path
import secrets
import subprocess
from uuid import uuid4

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from backend.tests.nginx_fixtures import free_port, bounded_test
from backend.tests.remote_tls_fixtures import certificates, tls_server
from backend.tests.test_security_core import credentials
from backend.tests.test_security_transport import Runtime
from backend.tests.test_session_transport import Service, body
from kulai_memory.app_factory import create_app
from kulai_memory.api.authentication import AuthenticationMiddleware
from kulai_memory.remote_ingress.boundary import RemoteBinding, RemoteIngressGuard
from kulai_memory.remote_ingress.tcp_bridge import BridgeConfig, TransparentBridge
from kulai_memory.security import Authenticator, SecurityConfig

SNIPPETS = Path(__file__).resolve().parents[3] / 'ops/remote_access/nginx'


class PublicTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        request.extensions['sni_hostname'] = 'demo.kulai.eu'
        return await super().handle_async_request(request)


@asynccontextmanager
async def isolated_gateway(certificates, directory, *, fault=None):
    logging.getLogger('httpx').setLevel(logging.WARNING)
    executable = os.environ.get('KULAI_TEST_NGINX')
    if not executable or not Path(executable).is_file():
        pytest.skip('Set KULAI_TEST_NGINX to the reviewed portable test binary')
    clock, store, _, device, credential = await credentials()
    runtime, renewals = Runtime(), Service()
    app = create_app(voice_runtime_factory=lambda: runtime,
        security_config=SecurityConfig(),
        authenticator=Authenticator(store, clock=lambda: clock[0]), session_service=renewals)
    binding = RemoteBinding(certificates.config(public_origin='https://demo.kulai.eu'), SecurityConfig())
    # Test readiness replaces only the DB checkpoint, never TLS peer verification.
    class Ready:
        remote_ready = True
        async def require_remote_ready(self): pass
    binding.runtime = Ready()
    app.state.remote_ingress_binding = binding
    for middleware in app.user_middleware:
        if middleware.cls is AuthenticationMiddleware:
            middleware.kwargs['remote_binding'] = binding
    app.add_middleware(RemoteIngressGuard, binding=binding)
    received = []
    class Trace:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            if scope['type'] in ('http', 'websocket'):
                received.append((scope['raw_path'], scope['query_string'], dict(scope['headers'])))
            await self.app(scope, receive, send)
    app.add_middleware(Trace)
    certificates.make('public', server=True, identity='demo.kulai.eu')
    async with tls_server(app) as (_, ingress):
        bridge = TransparentBridge(app, ingress, BridgeConfig('127.0.0.1', '127.0.0.1',
            'TEST-LOOPBACK', port=free_port(), test_loopback=True))
        await bridge.start()
        port = free_port()
        path = lambda name, suffix: str(certificates.path(name, suffix)).replace('\\', '/')
        http = (SNIPPETS / 'memory-path.http.conf').read_text(encoding='utf-8')
        server = (SNIPPETS / 'memory-path.server.conf').read_text(encoding='utf-8')
        for before, after in {
            'https://10.77.77.2:8443/': f'https://127.0.0.1:{bridge.config.port}/',
            'proxy_bind 10.77.77.1;': 'proxy_bind 127.0.0.1;',
            '/etc/kulai-memory/tls/gateway.pem': path('foreign' if fault == 'client' else 'gateway', 'pem'),
            '/etc/kulai-memory/tls/gateway.key': path('foreign' if fault == 'client' else 'gateway', 'key'),
            '/etc/kulai-memory/tls/ingress-ca.pem': path('other-ca' if fault == 'ca' else 'ca', 'pem'),
            'proxy_ssl_name kulai-memory-ingress;': 'proxy_ssl_name '+('wrong.test' if fault == 'name' else 'ingress.test')+';',
            '/var/log/nginx/kulai-memory-path-safe.log': 'safe.log',
            '/dev/null': 'nul' if os.name == 'nt' else '/dev/null',
        }.items():
            server = server.replace(before, after)
        (directory / 'memory.http.conf').write_text(http, encoding='utf-8')
        (directory / 'memory.server.conf').write_text(server, encoding='utf-8')
        (directory / 'logs').mkdir()
        (directory / 'temp').mkdir()
        config = f'''daemon off;
worker_processes 1;
pid nginx.pid;
error_log {'nul' if os.name == 'nt' else '/dev/null'} emerg;
events {{ worker_connections 128; }}
http {{
    include memory.http.conf;
    server {{
        listen 127.0.0.1:{port} ssl;
        server_name demo.kulai.eu;
        ssl_certificate "{path('public', 'pem')}";
        ssl_certificate_key "{path('public', 'key')}";
        access_log off;
        location /mentor/ {{ return 200 'mentor-unchanged'; }}
        location /propertly/ {{ return 200 'propertly-unchanged'; }}
        location / {{ return 200 'root-unchanged'; }}
        include memory.server.conf;
    }}
}}'''
        (directory / 'nginx.conf').write_text(config, encoding='utf-8')
        command = [executable, '-p', str(directory).replace('\\', '/')+'/', '-c', 'nginx.conf']
        process = None
        try:
            check = await asyncio.to_thread(subprocess.run, [*command, '-t'], capture_output=True, timeout=10)
            assert check.returncode == 0, check.stderr.decode(errors='replace')
            process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with httpx.AsyncClient(base_url=f'https://127.0.0.1:{port}', headers={'Host': 'demo.kulai.eu'},
                transport=PublicTransport(verify=certificates.client(None), trust_env=False, retries=0),
                timeout=5, follow_redirects=False, trust_env=False) as client:
                async with asyncio.timeout(10):
                    while True:
                        try:
                            if (await client.get('/')).status_code == 200: break
                        except httpx.TransportError: pass
                        await asyncio.sleep(.05)
                yield client, port, credential, device, received, renewals, runtime
        finally:
            if process is not None:
                await asyncio.to_thread(subprocess.run, [*command, '-s', 'stop'], capture_output=True, timeout=10)
                await asyncio.wait_for(process.wait(), 10)
            await bridge.close()


def bearer(credential, device):
    return {'Authorization': 'Bearer '+credential.access_token, 'X-Kulai-Device-ID': str(device.id)}


def test_prefix_http_wss_auth_headers_and_other_project_routes(certificates, tmp_path):
    async def run():
        async with isolated_gateway(certificates, tmp_path) as (client, port, credential, device, received, renewals, runtime):
            for route, text in [('/', 'root-unchanged'), ('/mentor/', 'mentor-unchanged'), ('/propertly/', 'propertly-unchanged')]:
                assert (await client.get(route)).text == text
            assert received == []
            assert (await client.get('/memory/api/memories')).status_code == 401
            auth = bearer(credential, device)
            forged = {**auth, 'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1',
                'X-Verified-Client': 'gateway.test', 'Cookie': 'private=synthetic', 'X-Arbitrary': 'synthetic'}
            assert (await client.get('/memory/api/memories?archived=false&limit=20', headers=forged)).status_code == 200
            raw, query, headers = received[-1]
            assert raw == b'/api/memories' and query == b'archived=false&limit=20'
            assert headers[b'host'] == b'demo.kulai.eu' and headers[b'authorization'] == auth['Authorization'].encode()
            assert not set(headers) & {b'forwarded', b'x-forwarded-for', b'x-verified-client', b'cookie', b'x-arbitrary'}
            for endpoint, payload in [('ask', {'query': 'Synthetic'}), ('speech-plan', {'answer': 'Synthetic answer'})]:
                assert (await client.post('/memory/api/assistant/'+endpoint, headers=auth, json=payload)).status_code == 200
                assert received[-1][0] == ('/api/assistant/'+endpoint).encode()
            detail = '/api/memories/'+str(uuid4())
            assert (await client.get('/memory'+detail, headers=auth)).status_code in (200, 404)
            assert received[-1][0] == detail.encode()
            for name in ('memory', 'assistant'):
                options = dict(host='127.0.0.1', port=port, server_hostname='demo.kulai.eu',
                    ssl=certificates.client(None), proxy=None, open_timeout=5, close_timeout=2)
                with pytest.raises(InvalidStatus):
                    async with connect('wss://demo.kulai.eu/memory/ws/'+name, **options): pass
                async with connect('wss://demo.kulai.eu/memory/ws/'+name, additional_headers=auth, **options):
                    assert received[-1][0] == ('/ws/'+name).encode()
            # Actual refresh parser/service is invoked exactly once after stripping.
            result = await client.post('/memory/api/security/refresh', json=body(),
                headers={'Authorization': 'Refresh krf1_'+secrets.token_urlsafe(32)})
            assert result.status_code == 401 and renewals.calls == 1
            assert received[-1][0] == b'/api/security/refresh'
            renewals.fail = True
            assert (await client.post('/memory/api/security/refresh', json=body(),
                headers={'Authorization': 'Refresh krf1_'+secrets.token_urlsafe(32)})).status_code == 503
            assert renewals.calls == 2  # A failed potentially writing POST is never retried.
            for operation in ('request', 'status', 'claim', 'cancel'):
                result = await client.post('/memory/api/pairing/'+operation, json={'session_id': str(uuid4())},
                    headers={'Authorization': 'Pairing '+secrets.token_urlsafe(32)})
                assert result.status_code in (400, 503)
                assert received[-1][0] == ('/api/pairing/'+operation).encode()
            for route, text in [('/', 'root-unchanged'), ('/mentor/', 'mentor-unchanged'), ('/propertly/', 'propertly-unchanged')]:
                assert (await client.get(route)).text == text
        log = (tmp_path / 'safe.log').read_text()
        assert credential.access_token not in log and '/api/' not in log and '/memory/' not in log
    asyncio.run(bounded_test(run()))


def test_raw_path_and_method_allowlist_blocks_normalization_and_browser(certificates, tmp_path):
    async def run():
        async with isolated_gateway(certificates, tmp_path) as (client, port, credential, device, received, _, _):
            paths = ['/memory', '/memory/', '/memory/health', '/memory/docs', '/memory/api/pairing/approve',
                '/memory/api/memories/', '/memory/api/%6demories', '/memory//api/memories',
                '/memory/ws/memory?access_token=synthetic', '/memory/api/security/refresh?x=1',
                '/memory/api/memories/'+str(uuid4())+'?x=1']
            auth = bearer(credential, device)
            for path in paths:
                assert (await client.get(path, headers=auth)).status_code == 404
            # Raw TLS bytes preserve dot segments that HTTP clients normalize away.
            for path in [b'/memory/../mentor/', b'/memory/%2e%2e/propertly/', b'/memory%2f..%2fmentor/']:
                reader, writer = await asyncio.open_connection('127.0.0.1', port,
                    ssl=certificates.client(None), server_hostname='demo.kulai.eu')
                try:
                    writer.write(b'GET '+path+b' HTTP/1.1\r\nHost: demo.kulai.eu\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                    assert (await reader.readline()).split()[1] == b'404'
                    await reader.read()
                finally:
                    writer.close()
                    try: await writer.wait_closed()
                    except OSError: pass  # Windows may reset after a fully read close response.
            for method, path in [('HEAD', '/memory/api/memories'), ('POST', '/memory/api/memories'),
                ('GET', '/memory/api/assistant/ask'), ('GET', '/memory/api/pairing/status'),
                ('GET', '/memory/api/security/refresh'), ('POST', '/memory/ws/memory'), ('OPTIONS', '/memory/api/memories')]:
                assert (await client.request(method, path, headers=auth)).status_code == 404
            assert (await client.get('/memory/api/memories', headers={**auth, 'Origin': 'https://demo.kulai.eu'})).status_code == 403
            assert (await client.get('/memory/api/memories', headers={**auth, 'Host': 'evil.test'})).status_code == 421
            assert received == []
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('fault', ['ca', 'name', 'client'])
def test_mtls_fails_closed_without_upstream_requests_or_retries(certificates, tmp_path, fault):
    async def run():
        async with isolated_gateway(certificates, tmp_path, fault=fault) as (client, _, credential, device, received, renewals, _):
            response = await client.post('/memory/api/security/refresh', json=body(), headers=bearer(credential, device))
            assert response.status_code == 503 and received == [] and renewals.calls == 0
            assert 'request_applied' not in response.json()
            assert (await client.get('/mentor/')).text == 'mentor-unchanged'
    asyncio.run(bounded_test(run()))
