"""Narrow pre-auth exception and parsing/cancellation, without any database or AI."""
import asyncio
from collections import deque
import json
import secrets
from uuid import uuid4
import pytest
from backend.tests.test_security_transport import Runtime, no_private_work
from backend.tests.local_transport import TestClient
from kulai_memory.app_factory import create_app
from kulai_memory.security import SecurityConfig, SecurityMode
from kulai_memory.security.storage import DenyAllSecurityReader
from kulai_memory.pairing.domain import PairingError, label, digest
from kulai_memory.api.pairing_http import create_pairing_router
from kulai_memory.api.authentication import AuthenticationMiddleware
from kulai_memory.security import Authenticator, AuthorizationPolicy


@pytest.mark.parametrize('value', ['', 'x'*49, 'Phone\nSECRET', 'Phone;command', '🔥', None])
def test_labels_refuse_control_payloads(value):
    with pytest.raises(PairingError) as error:
        label(value)
    assert str(error.value) == 'Pairing could not be completed.'


@pytest.mark.parametrize('value', ['', 'Bearer x', 'x'*42, 'x'*44, None])
def test_bad_proof_format_is_safe(value):
    with pytest.raises(PairingError) as error:
        digest(value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize('variant', ['get', 'suffix', 'ws', 'query', 'proxy', 'origin', 'duplicates', 'local_dev'])
def test_only_exact_native_secure_pairing_post_is_exempt(variant):
    class Service:
        calls = 0
        async def status(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError('PRIVATE_PAIRING_SERVICE')
    service = Service(); runtime = Runtime()
    mode = SecurityMode.LOCAL_DEV if variant == 'local_dev' else SecurityMode.SECURE
    app = create_app(voice_runtime_factory=lambda: runtime, security_config=SecurityConfig(mode=mode),
        security_reader=DenyAllSecurityReader(), pairing_service=service)
    with TestClient(app) as client:
        headers = {'Authorization': 'Pairing ' + secrets.token_urlsafe(32)}
        route = '/api/pairing/status'
        if variant == 'proxy': headers['X-Forwarded-For'] = '127.0.0.1'
        if variant == 'origin': headers['Origin'] = 'http://127.0.0.1'
        if variant == 'suffix': route += '/extra'
        if variant == 'query': route += '?challenge=forbidden'
        if variant == 'duplicates': headers = [('Authorization', headers['Authorization']), ('Authorization', headers['Authorization'])]
        if variant == 'ws':
            with pytest.raises(Exception):
                with client.websocket_connect(route, headers=headers):
                    pytest.fail('Pairing accepted WebSocket.')
        else:
            response = client.get(route, headers=headers) if variant == 'get' else client.post(route, headers=headers, json={'session_id': str(uuid4())})
            assert response.status_code in (400, 401, 403)
            assert 'PRIVATE' not in response.text
        assert service.calls == 0
        no_private_work(runtime)


def test_disconnect_cancels_pairing_operation_without_private_work():
    async def scenario():
        entered, drained = asyncio.Event(), asyncio.Event()
        class Service:
            async def status(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
        from fastapi import FastAPI
        app = FastAPI(); app.include_router(create_pairing_router(service=Service()))
        middleware = AuthenticationMiddleware(app, config=SecurityConfig(mode=SecurityMode.SECURE),
            authenticator=Authenticator(DenyAllSecurityReader()), policy=AuthorizationPolicy())
        scope = {'type':'http','asgi':{'version':'3.0'},'method':'POST','path':'/api/pairing/status','raw_path':b'/api/pairing/status',
            'query_string':b'','scheme':'http','server':('127.0.0.1',8000),'client':('127.0.0.1',50001),
            'headers':[(b'host',b'127.0.0.1:8000'),(b'authorization',('Pairing '+secrets.token_urlsafe(32)).encode()),(b'content-type',b'application/json')],
            'http_version':'1.1'}
        count = 0
        async def receive():
            nonlocal count
            count += 1
            if count == 1:
                return {'type':'http.request','body':json.dumps({'session_id':str(uuid4())}).encode(),'more_body':False}
            await entered.wait()
            return {'type':'http.disconnect'}
        messages = []
        async def send(message): messages.append(message)
        await asyncio.wait_for(middleware(scope, receive, send), 2)
        assert drained.is_set()
        assert all(b'PRIVATE' not in m.get('body',b'') for m in messages)
    asyncio.run(scenario())


@pytest.mark.parametrize('content_type', ['application/json', 'application/json; charset=utf-8'])
def test_android_json_charset_reaches_pairing_without_reflecting_errors(content_type):
    class Service:
        calls = 0
        async def status(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError('PRIVATE_FAULT')
    service = Service(); runtime = Runtime()
    app = create_app(voice_runtime_factory=lambda: runtime, security_reader=DenyAllSecurityReader(), pairing_service=service)
    with TestClient(app) as client:
        response = client.post('/api/pairing/status', content=json.dumps({'session_id':str(uuid4())}),
            headers={'Content-Type':content_type,'Authorization':'Pairing '+secrets.token_urlsafe(32)})
        assert service.calls == 1 and response.status_code == 400 and 'PRIVATE_FAULT' not in response.text
        no_private_work(runtime)
