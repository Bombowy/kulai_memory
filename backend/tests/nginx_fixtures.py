"""Actual isolated Nginx process; no existing vhost/service is touched."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
import logging
from pathlib import Path
import socket
import subprocess
import tempfile

import httpx
import pytest
from websockets.asyncio.client import connect

from backend.tests.integration.test_remote_ingress_tls import headers
from backend.tests.remote_tls_fixtures import tls_server
from kulai_memory.remote_ingress.tcp_bridge import BridgeConfig, TransparentBridge
from ops.remote_access.package import Deployment


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]


async def bounded_test(coroutine):
    # Deadlines also cancel the fixture owner, which drains sockets and drops DB.
    async with asyncio.timeout(40):
        return await coroutine


class PublicTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self,request):
        # Test socket uses loopback; real TLS SNI + hostname check uses public SAN.
        request.extensions['sni_hostname']='memory.test'
        return await super().handle_async_request(request)


class Gateway:
    def __init__(self,app,ingress,certificates,directory):
        self.certificates=certificates
        self.directory=Path(directory)
        (self.directory/'logs').mkdir(exist_ok=True)
        (self.directory/'temp').mkdir(exist_ok=True)
        self.app,self.ingress=app,ingress
        self.port=free_port()
        self.bridge=TransparentBridge(app,ingress,BridgeConfig('127.0.0.1','127.0.0.1',
            'TEST-LOOPBACK',port=free_port(),test_loopback=True))
        executable=os.environ.get('KULAI_TEST_NGINX')
        if not executable or not Path(executable).is_file():
            pytest.skip('NOT RUN: set KULAI_TEST_NGINX to reviewed isolated Nginx binary')
        self.executable=executable
        certificates.make('public',server=True,identity='memory.test')
        path=lambda name,suffix:str(certificates.path(name,suffix)).replace('\\','/')
        self.deployment=Deployment(public_origin='https://memory.test',public_listen_address='127.0.0.1',
            windows_wg_ip='127.0.0.1',vps_wg_ip='127.0.0.1',vps_endpoint='vps.test',
            ingress_identity='ingress.test',public_certificate=path('public','pem'),public_private_key=path('public','key'),
            gateway_certificate=path('gateway','pem'),gateway_private_key=path('gateway','key'),ingress_ca=path('ca','pem'),
            access_log=str(self.directory/'safe.log').replace('\\','/'),bridge_port=self.bridge.config.port,
            test_loopback=True,test_listen_port=self.port)
        self.process=None
        self.generation=0

    def client(self,version=None):
        logging.getLogger('httpx').setLevel(logging.WARNING)
        context=self.certificates.client(None)
        if version is not None:
            context.minimum_version=context.maximum_version=version
        return httpx.AsyncClient(base_url=f'https://127.0.0.1:{self.port}',headers={'Host':'memory.test'},
            transport=PublicTransport(verify=context,trust_env=False,retries=0),
            timeout=10,follow_redirects=False,trust_env=False)

    def websocket(self,path,credential=None,**kwargs):
        return connect('wss://memory.test'+path,host='127.0.0.1',port=self.port,server_hostname='memory.test',
            ssl=self.certificates.client(None),proxy=None,additional_headers=headers(credential) if credential else {},
            open_timeout=10,close_timeout=3,**kwargs)

    async def start(self,**changes):
        deployment=replace(self.deployment,**changes)
        for name,body in deployment.candidates().items():
            (self.directory/name).write_text(body,encoding='utf-8',newline='\n')
        deployment.validate_candidates(self.directory)
        other=getattr(self,'other_vhosts','')
        config=('daemon off;\nworker_processes 1;\npid nginx.pid;\n'
                'error_log '+('nul' if os.name=='nt' else '/dev/null')+' emerg;\n'
                'events { worker_connections 128; }\nhttp { '+other+'\ninclude kulai-memory.nginx.conf; }\n')
        (self.directory/'nginx.conf').write_text(config,encoding='utf-8',newline='\n')
        prefix=str(self.directory).replace('\\','/')+'/'
        command=[self.executable,'-p',prefix,'-c','nginx.conf']
        check=await asyncio.to_thread(subprocess.run,[*command,'-t'],capture_output=True,timeout=10)
        assert check.returncode==0, check.stderr.decode('utf-8',errors='replace')
        self.process=await asyncio.create_subprocess_exec(*command,cwd=self.directory,
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        self.generation+=1
        try:
            async with asyncio.timeout(10):
                while True:
                    if self.process.returncode is not None:
                        raise RuntimeError('Isolated Nginx exited')
                    try:
                        async with self.client() as transport:
                            response=await transport.get('/health')
                            if response.status_code==404:
                                break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.05)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self.process is None:
            return
        prefix=str(self.directory).replace('\\','/')+'/'
        # Isolated prefix+PID only; never use taskkill /IM or killall nginx.
        result=await asyncio.to_thread(subprocess.run,[self.executable,'-p',prefix,'-c','nginx.conf','-s','stop'],
            capture_output=True,timeout=10)
        await asyncio.wait_for(self.process.wait(),10)
        self.process=None
        assert result.returncode==0


@asynccontextmanager
async def gateway_server(app,certificates):
    with tempfile.TemporaryDirectory(prefix='kulai-owned-nginx-config-') as directory:
        async with tls_server(app) as (_,ingress):
            gateway=Gateway(app,ingress,certificates,directory)
            try:
                await gateway.bridge.start()
                await gateway.start()
                yield gateway
            finally:
                await gateway.stop()
                await gateway.bridge.close()
