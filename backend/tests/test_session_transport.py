"""Exact refresh exception, bounded parsing and disconnect ownership; no database/AI."""
import asyncio
import json
from uuid import uuid4
import pytest
from backend.tests.local_transport import TestClient
from backend.tests.test_security_transport import Runtime, HTTP, WS, no_private_work
from kulai_memory.app_factory import create_app
from kulai_memory.security import SecurityConfig, SecurityMode
from kulai_memory.security.storage import DenyAllSecurityReader
from kulai_memory.security.sessions import RenewalResult, new_refresh, refresh_digest


class Service:
    calls = 0
    def __init__(self, *, fail=False, limited=False):
        self.fail, self.limited = fail, limited
    async def renew(self, *args):
        self.calls += 1
        if self.fail:
            raise RuntimeError('PRIVATE_CREDENTIAL_STORAGE')
        return RenewalResult(retry_later=self.limited)


def fixture(service=None, mode=SecurityMode.SECURE):
    runtime = Runtime()
    app = create_app(voice_runtime_factory=lambda: runtime, security_reader=DenyAllSecurityReader(),
        security_config=SecurityConfig(mode=mode), session_service=service)
    return app, runtime


def body():
    return {key: str(uuid4()) for key in ('session_id', 'device_id', 'refresh_id')}


@pytest.mark.parametrize('variant', ['get', 'suffix', 'slash', 'query', 'proxy', 'origin', 'host', 'duplicate_header',
    'bearer', 'access_refresh', 'duplicate_json', 'unknown_field', 'large', 'uuid', 'identity_header', 'local_dev', 'ws'])
def test_refresh_has_no_policy_or_parser_bypass(variant):
    service = Service()
    app, runtime = fixture(service, SecurityMode.LOCAL_DEV if variant == 'local_dev' else SecurityMode.SECURE)
    _, secret, _ = new_refresh()
    headers = {'Authorization': 'Refresh '+secret, 'Content-Type': 'application/json'}
    payload = json.dumps(body())
    path = '/api/security/refresh'
    if variant == 'suffix': path += '/issue'
    if variant == 'slash': path += '/'
    if variant == 'query': path += '?refresh='+secret
    if variant == 'proxy': headers['Forwarded'] = 'for=127.0.0.1'
    if variant == 'origin': headers['Origin'] = 'https://example.invalid'
    if variant == 'host': headers['Host'] = 'example.invalid'
    if variant == 'bearer': headers['Authorization'] = 'Bearer '+secret
    if variant == 'access_refresh': headers['Authorization'] = 'Refresh '+'a'*43
    if variant == 'duplicate_header': headers = list(headers.items())+[('Authorization', 'Refresh '+secret)]
    if variant == 'duplicate_json': payload = '{"session_id":"PRIVATE","session_id":"x"}'
    if variant == 'unknown_field': payload = json.dumps({**body(), 'actions': ['memory.write']})
    if variant == 'large': payload = ' '*4097
    if variant == 'uuid': payload = json.dumps({**body(), 'device_id': str(uuid4()).upper()})
    if variant == 'identity_header': headers['X-Kulai-Project-ID'] = str(uuid4())
    with TestClient(app) as client:
        if variant == 'ws':
            with pytest.raises(Exception):
                with client.websocket_connect(path, headers=headers): pytest.fail('Refresh WS accepted.')
        else:
            response = client.request('GET' if variant == 'get' else 'POST', path, headers=headers, content=payload)
            assert response.status_code in (400, 401, 403)
            assert 'PRIVATE' not in response.text and secret not in response.text
        assert service.calls == 0
        no_private_work(runtime)


@pytest.mark.parametrize('kind,status', [('denied', 401), ('limited', 429), ('failure', 503), ('absent', 503)])
def test_safe_renewal_errors_and_no_ai(kind, status, caplog):
    service = None if kind == 'absent' else Service(fail=kind == 'failure', limited=kind == 'limited')
    app, runtime = fixture(service)
    _, secret, digest = new_refresh()
    assert secret not in repr(digest) and refresh_digest(secret) == digest
    with TestClient(app) as client:
        response = client.post('/api/security/refresh', json=body(), headers={'Authorization': 'Refresh '+secret})
        assert response.status_code == status and response.headers['Cache-Control'] == 'no-store'
        assert (response.json().get('request_applied') is False) == (kind in ('limited', 'absent'))
        assert secret not in response.text+caplog.text and 'PRIVATE_CREDENTIAL_STORAGE' not in response.text+caplog.text
        no_private_work(runtime)


@pytest.mark.parametrize('path', [route for _, route in HTTP]+list(WS))
def test_refresh_secret_never_authenticates_memory(path):
    app, runtime = fixture(Service())
    _, secret, _ = new_refresh()
    with TestClient(app) as client:
        if path in WS:
            with pytest.raises(Exception):
                with client.websocket_connect(path, headers={'Authorization': 'Bearer '+secret}): pytest.fail()
        else:
            method = next(method for method, route in HTTP if route == path)
            assert client.request(method, path, headers={'Authorization': 'Bearer '+secret}).status_code == 401
        no_private_work(runtime)


def test_disconnect_drains_pending_refresh_without_retransmission():
    async def scenario():
        from fastapi import FastAPI
        from kulai_memory.api.session_http import create_session_router
        from kulai_memory.api.authentication import AuthenticationMiddleware
        from kulai_memory.security import Authenticator, AuthorizationPolicy
        entered, drained = asyncio.Event(), asyncio.Event()
        class Blocked:
            async def renew(self, *args):
                entered.set()
                try: await asyncio.Event().wait()
                finally: drained.set()
        app = FastAPI(); app.include_router(create_session_router(service=Blocked()))
        middleware = AuthenticationMiddleware(app, config=SecurityConfig(),
            authenticator=Authenticator(DenyAllSecurityReader()), policy=AuthorizationPolicy())
        _, secret, _ = new_refresh()
        scope = {'type':'http', 'asgi':{'version':'3.0'}, 'method':'POST', 'path':'/api/security/refresh',
            'raw_path':b'/api/security/refresh', 'query_string':b'', 'scheme':'http', 'http_version':'1.1',
            'server':('127.0.0.1',8000), 'client':('127.0.0.1',40000), 'headers':[(b'host', b'127.0.0.1:8000'),
            (b'authorization', ('Refresh '+secret).encode()), (b'content-type', b'application/json')]}
        delivered = False
        async def receive():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {'type':'http.request', 'body':json.dumps(body()).encode(), 'more_body':False}
            await entered.wait()
            return {'type':'http.disconnect'}
        messages=[]
        async def send(message): messages.append(message)
        await asyncio.wait_for(middleware(scope, receive, send), 2)
        assert drained.is_set() and all(secret.encode() not in m.get('body', b'') for m in messages)
    asyncio.run(scenario())
