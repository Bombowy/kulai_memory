"""Fresh temporary PKI and real loopback TCP/TLS; no production keys or fake peers."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
import socket
import ssl
import tempfile

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from kulai_memory.remote_ingress import RemoteIngressConfig, RemoteIngressError
from kulai_memory.remote_ingress.server import create_remote_server


class Certificates:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.now = datetime.now(timezone.utc)
        self.ca, self.ca_key = self.root('ca')
        self.other_ca, self.other_key = self.root('other-ca')
        self.make('server', server=True)
        self.make('gateway')
        self.make('foreign', other=True)
        self.make('wrong-san', identity='untrusted.test')
        self.make('expired', expired=True)
        self.make('wrong-purpose', server=True, identity='gateway.test')
        self.make('wrong-server', server=True, other=True)
        self.make('wrong-server-name', server=True, identity='other.test', ip=False)

    def root(self, name, days=2):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(self.now-timedelta(days=1)).not_valid_after(self.now+timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, None, None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
        self.path(name, 'pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert, key

    def make(self, name, *, server=False, identity=None, ip=True, other=False, expired=False, days=1):
        key = ec.generate_private_key(ec.SECP256R1())
        ca, issuer_key = (self.other_ca, self.other_key) if other else (self.ca, self.ca_key)
        identity = identity or ('ingress.test' if server else 'gateway.test')
        san = [x509.DNSName(identity)]
        if server and ip:
            san.append(x509.IPAddress(ip_address('127.0.0.1')))
        cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(self.now-timedelta(days=2 if expired else 1))
            .not_valid_after(self.now-timedelta(days=1) if expired else self.now+timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, None, None), critical=True)
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
            .sign(issuer_key, hashes.SHA256()))
        self.path(name, 'pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.path(name, 'key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    def path(self, name, suffix):
        return self.directory / (name+'.'+suffix)

    def config(self, **changes):
        return replace(RemoteIngressConfig(enabled=True, public_origin='https://memory.test',
            expected_gateway_identity='gateway.test', certificate=self.path('server', 'pem'),
            private_key=self.path('server', 'key'), client_ca=self.path('ca', 'pem'),
            recovery_gate=self.directory/'gate'), **changes)

    def client(self, name='gateway'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cafile=str(self.path('ca', 'pem')))
        if name is not None:
            context.load_cert_chain(str(self.path(name, 'pem')), str(self.path(name, 'key')))
        return context


@pytest.fixture
def certificates():
    with tempfile.TemporaryDirectory(prefix='kulai-owned-mtls-') as directory:
        yield Certificates(directory)


@asynccontextmanager
async def tls_server(app):
    await prepare_owned_gate(app)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        host = create_remote_server(app, port=port)
        task = asyncio.create_task(host.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not host.started:
                    if task.done():
                        await task
                        raise RemoteIngressError()
                    await asyncio.sleep(.01)
            yield port, host
        finally:
            host.should_exit = True
            await asyncio.wait_for(task, 10)


async def prepare_owned_gate(app):
    """Explicit synthetic bootstrap with actual owned capability; never production.

    Existing closed/pending gates are NEVER reopened by the fixture. The initial
    pre-approved test state is checkpointed only for a freshly created owned DB.
    """
    fixture = getattr(app.state, '_owned_gate_fixture', None)
    if fixture is None: return
    from sqlalchemy import text
    from kulai_memory.database_safety import require_owned_database
    from kulai_memory.security_persistence.integrity import inspect_security_schema
    from kulai_memory.security_recovery.gate import RecoveryGate, checkpoint, locked
    owned, config, target, factory = fixture
    await require_owned_database(owned, config=config)
    path = app.state.remote_ingress_binding.config.recovery_gate
    async with factory() as session:
        async with session.begin():
            await session.execute(text('SET TRANSACTION READ ONLY'))
            state = await inspect_security_schema(await session.connection())
            if not state.valid or state.revision != 'kulai_security_0003': return
            value = await checkpoint(await session.connection())
    if not path.exists():
        gate = RecoveryGate.initialize(path)
        async with locked(gate.mutex):
            gate.append({'state':'OPEN','identity':value[0],'security':value[1],'synthetic_owned_bootstrap':True})
    else:
        gate = RecoveryGate(path)
    factory.configure(info={'recovery_gate':gate})
