"""Configuration and capability isolation; real TLS is tested separately."""
from dataclasses import replace

import pytest
from backend.tests.remote_tls_fixtures import certificates
from backend.tests.local_transport import TestClient, create_local_app
from backend.tests.test_security_transport import Runtime
from kulai_memory.app_factory import create_app
from kulai_memory.remote_ingress import RemoteIngressConfig, RemoteIngressError
from kulai_memory.remote_ingress.server import create_remote_server
from kulai_memory.security import Authenticator
from kulai_memory.security.storage import DenyAllSecurityReader


@pytest.mark.parametrize('field,value', [('public_origin','http://memory.test'),('public_origin','https://user:password@memory.test'),
    ('public_origin','https://memory.test/path'),('public_origin','https://memory.test?secret=x'),
    ('public_origin','https://memory.test:99999'),('expected_gateway_identity','*.test'),
    ('public_origin','https://memory.test:0'),('public_origin','https://memory.test:'),
    ('expected_gateway_identity',''),('gateway_sha256','invalid'),('max_body_bytes',65537),
    ('max_frame_bytes',262145),('http_timeout',0),('idle_timeout',float('nan'))])
def test_invalid_configuration_is_sanitized(certificates,field,value):
    with pytest.raises(RemoteIngressError) as error:
        replace(certificates.config(),**{field:value})
    assert 'password' not in str(error.value) and 'secret=x' not in str(error.value)


def test_default_disabled_local_dev_unchanged_and_headers_cannot_create_remote_peer():
    assert not RemoteIngressConfig().enabled
    runtime = Runtime()
    with TestClient(create_local_app(voice_runtime_factory=lambda:runtime)) as client:
        assert client.get('/api/memories').status_code == 200
    with pytest.raises(RemoteIngressError):
        create_remote_server(create_app(),port=8443)


def test_remote_requires_managed_security_and_real_tls_scope_even_on_loopback(certificates):
    with pytest.raises(RemoteIngressError):
        create_app(remote_ingress_config=certificates.config(),authenticator=Authenticator(DenyAllSecurityReader()))
    app = create_app(remote_ingress_config=certificates.config())
    # No lifespan/DB: a header cannot create a peer even on real loopback scope.
    client = TestClient(app)
    try:
        assert client.get('/api/memories',headers={'X-Verified-Client':'gateway.test'}).status_code == 403
    finally:
        client.close()


@pytest.mark.parametrize('kind',['mismatched','encrypted'])
def test_key_errors_refuse_before_listen_without_interactive_password_prompt(certificates,kind):
    if kind == 'mismatched':
        path = certificates.path('gateway','key')
    else:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(certificates.path('server','key').read_bytes(),password=None)
        path = certificates.path('encrypted-server','key')
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b'synthetic-test-password')))
    config = replace(certificates.config(),private_key=path)
    with pytest.raises(RemoteIngressError) as error:
        config.tls_context()
    assert 'synthetic-test-password' not in str(error.value)
