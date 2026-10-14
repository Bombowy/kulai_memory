"""Actual TLS routing across three vhosts on a single isolated loopback IP:port."""
import asyncio
from dataclasses import replace
import httpx
from backend.tests.integration.test_nginx_gateway import pipeline
from backend.tests.nginx_fixtures import bounded_test
from backend.tests.remote_tls_fixtures import certificates


def test_real_shared_ip_sni_host_isolation_preserves_other_sites(certificates):
    async def run():
        async with pipeline(certificates) as (g,_,runtime,_,_,credential):
            await g.stop()
            certificates.make('other-site',server=True,identity='other.test')
            certificate=str(certificates.path('other-site','pem')).replace('\\','/')
            key=str(certificates.path('other-site','key')).replace('\\','/')
            listener=f'127.0.0.1:{g.port}'
            g.other_vhosts=f'''server {{ listen {listener} ssl default_server; server_name _;
                ssl_protocols TLSv1.2 TLSv1.3; ssl_reject_handshake on; access_log off; return 421; }}
                server {{ listen {listener} ssl; server_name other.test;
                ssl_certificate "{certificate}"; ssl_certificate_key "{key}"; access_log off;
                return 200 'other-project'; }}'''
            await g.start(shared_ip=True)
            assert ' ssl default_server;' not in (g.directory/'kulai-memory.nginx.conf').read_text()
            from backend.tests.integration.test_remote_ingress_tls import headers
            async with g.client() as client:
                assert (await client.get('/api/memories',headers=headers(credential))).status_code==200
            class SNI(httpx.AsyncHTTPTransport):
                def __init__(self,name):
                    self.name=name
                    super().__init__(verify=certificates.client(None),trust_env=False,retries=0)
                async def handle_async_request(self,request):
                    request.extensions['sni_hostname']=self.name
                    return await super().handle_async_request(request)
            for sni,host,status,text in [('other.test','other.test',200,'other-project'),
                                         ('other.test','memory.test',421,None),
                                         ('memory.test','unknown.test',421,None)]:
                async with httpx.AsyncClient(transport=SNI(sni),trust_env=False,timeout=5) as client:
                    response=await client.get(f'https://127.0.0.1:{g.port}/api/memories',headers={'Host':host})
                    assert response.status_code==status
                    if text: assert response.text==text
            import pytest
            with pytest.raises(httpx.TransportError):
                async with httpx.AsyncClient(transport=SNI('unknown.test'),trust_env=False,timeout=5) as client:
                    await client.get(f'https://127.0.0.1:{g.port}/api/memories',headers={'Host':'memory.test'})
            # WSS uses exactly the same shared listener and retained mTLS upstream.
            async with g.websocket('/ws/memory',credential) as ws:
                await ws.close()
            # Existing project's configuration is byte-for-byte retained on restart.
            before=g.other_vhosts
            await g.stop(); await g.start(shared_ip=True)
            assert g.other_vhosts==before
            async with httpx.AsyncClient(transport=SNI('other.test'),trust_env=False,timeout=5) as client:
                assert (await client.get(f'https://127.0.0.1:{g.port}/',headers={'Host':'other.test'})).text=='other-project'
    asyncio.run(bounded_test(run()))
