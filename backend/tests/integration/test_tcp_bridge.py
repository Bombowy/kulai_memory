"""Actual opaque TCP bridge, real endpoint mTLS, owned security v3 PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import ssl
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from websockets.exceptions import ConnectionClosed

from backend.tests.nginx_fixtures import free_port,bounded_test
from backend.tests.remote_tls_fixtures import certificates,tls_server
from backend.tests.integration.test_remote_ingress_tls import application,headers,client,websocket
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_sessions_postgres import paired
from backend.tests.test_memory_ws import _start_message
from kulai_memory.remote_ingress import RemoteIngressError
from kulai_memory.remote_ingress.tcp_bridge import BridgeConfig,TransparentBridge
from kulai_memory.security import Action


@asynccontextmanager
async def bridge_pipeline(certificates,**changes):
    async with owned_store() as (_,_,target,factory,_):
        _,_,_,_,credential=await paired(factory,actions=frozenset(Action))
        app,runtime=application(target,factory,certificates.config())
        async with tls_server(app) as (_,server):
            config=replace(BridgeConfig('127.0.0.1','127.0.0.1','TEST-LOOPBACK',port=free_port(),test_loopback=True),**changes)
            bridge=TransparentBridge(app,server,config)
            try:
                port=await bridge.start()
                yield port,app,runtime,bridge,credential,factory
            finally:
                await bridge.close()


def test_bridge_preserves_real_tls_certificate_and_bearer_requirement(certificates):
    async def run():
        async with bridge_pipeline(certificates) as (port,_,_,bridge,credential,_):
            async with client(certificates,port) as transport:
                assert (await transport.get('/api/memories')).status_code==401
                response=await transport.get('/api/memories',headers=headers(credential))
                assert response.status_code==200
                tls=response.extensions['network_stream'].get_extra_info('ssl_object')
                assert tls.getpeercert(binary_form=True)==ssl.PEM_cert_to_DER_cert(certificates.path('server','pem').read_text())
            # Plain HTTP never reaches a LOCAL_DEV application through this opaque bridge.
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=3) as transport:
                with pytest.raises(httpx.TransportError):
                    await transport.get('/api/memories',headers={'Host':'localhost:8443'})
    asyncio.run(bounded_test(run()))


@pytest.mark.parametrize('changes',[{'peer_address':'127.0.0.2'},{'max_connections':1},{'idle_timeout':.15},{'lifetime':.15}])
def test_bridge_peer_capacity_idle_and_total_limits(certificates,changes):
    async def run():
        async with bridge_pipeline(certificates,**changes) as (port,_,_,bridge,credential,_):
            reader,writer=await asyncio.open_connection('127.0.0.1',port)
            try:
                if 'max_connections' in changes:
                    async with asyncio.timeout(3):
                        while not bridge.tasks: await asyncio.sleep(.01)
                    async with client(certificates,port) as transport:
                        with pytest.raises(httpx.TransportError):
                            await transport.get('/api/memories',headers=headers(credential))
                else:
                    try:
                        assert await asyncio.wait_for(reader.read(1),3)==b''
                    except ConnectionResetError:
                        pass
            finally:
                writer.transport.abort()
            await bridge.close()
            assert not bridge.tasks and not bridge.writers
    asyncio.run(bounded_test(run()))


def test_bridge_restart_and_secure_runtime_latch_close_access(certificates):
    async def run():
        async with bridge_pipeline(certificates) as (port,app,_,bridge,credential,_):
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await ws.send(json.dumps(_start_message(uuid4())))
                await ws.recv()
                await bridge.close()
                with pytest.raises(ConnectionClosed): await ws.recv()
            await bridge.start()
            async with client(certificates,port) as transport:
                assert (await transport.get('/api/memories',headers=headers(credential))).status_code==200
            app.state.security_runtime._remote_closed=True
            async with client(certificates,port) as transport:
                with pytest.raises(httpx.TransportError): await transport.get('/api/memories',headers=headers(credential))
            await bridge.close()
            with pytest.raises(RemoteIngressError): await bridge.start()
    asyncio.run(bounded_test(run()))


def test_bridge_cannot_target_a_different_local_dev_listener(certificates):
    from types import SimpleNamespace
    from kulai_memory.app_factory import create_app
    from kulai_memory.security import SecurityConfig,SecurityMode
    app=create_app(security_config=SecurityConfig(mode=SecurityMode.LOCAL_DEV))
    server=SimpleNamespace(config=SimpleNamespace(app=app,port=8000),started=True,should_exit=False)
    bridge=TransparentBridge(app,server,BridgeConfig('127.0.0.1','127.0.0.1','TEST-LOOPBACK',port=free_port(),test_loopback=True))
    async def run():
        with pytest.raises(RemoteIngressError): await bridge.start()
        assert bridge.listener is None
    asyncio.run(bounded_test(run()))


def test_combined_launcher_stops_both_listeners_on_interface_monitor_failure(certificates,monkeypatch):
    from kulai_memory.remote_ingress import tcp_bridge
    from kulai_memory.remote_ingress.server import create_remote_server
    calls=[]
    async def simulated_check(config):
        calls.append(True)
        if len(calls)>1: raise RemoteIngressError()
    # Tests the monitor's fail-closed control flow, NOT a real WireGuard interface.
    monkeypatch.setattr(tcp_bridge,'verify_windows_interface',simulated_check)
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            app,_=application(target,factory,certificates.config())
            ingress=create_remote_server(app,port=free_port())
            config=BridgeConfig('127.0.0.1','127.0.0.1','TEST-LOOPBACK',port=free_port(),test_loopback=True)
            with pytest.raises(RemoteIngressError):
                await tcp_bridge.serve_with_bridge(app,ingress,config)
            assert len(calls)==2 and ingress.should_exit
            for port in (config.port,ingress.config.port):
                with pytest.raises(OSError):
                    await asyncio.open_connection('127.0.0.1',port)
    asyncio.run(bounded_test(run()))
