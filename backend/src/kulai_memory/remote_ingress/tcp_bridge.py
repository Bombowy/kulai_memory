"""Bounded opaque TCP forwarding to the ingress owned by this very process.

No TLS, HTTP or WebSocket parser lives here. The end-to-end SSLObject still
belongs to Uvicorn. No arbitrary localhost destination or standalone proxy.
"""
import asyncio
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_network
import json
import os
from pathlib import Path

from .configuration import RemoteIngressError


@dataclass(frozen=True)
class BridgeConfig:
    listen_address: str
    peer_address: str
    interface_alias: str
    port: int = 8443
    test_loopback: bool = False
    max_connections: int = 32
    idle_timeout: float = 240
    lifetime: float = 900

    def __post_init__(self):
        try:
            local, peer = IPv4Address(self.listen_address), IPv4Address(self.peer_address)
            private = tuple(ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
            if type(self.test_loopback) is not bool or type(self.port) is not int or not 1 <= self.port <= 65535:
                raise ValueError()
            if self.test_loopback:
                if not local.is_loopback or not peer.is_loopback or self.interface_alias != 'TEST-LOOPBACK':
                    raise ValueError()
            elif (local == peer or not any(local in net for net in private) or
                  not any(peer in net for net in private) or not self.interface_alias or
                  len(self.interface_alias) > 64 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in self.interface_alias)):
                raise ValueError()
            if type(self.max_connections) is not int or not 1 <= self.max_connections <= 32:
                raise ValueError()
            for value, maximum in ((self.idle_timeout, 240), (self.lifetime, 900)):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
                    raise ValueError()
        except Exception:
            raise RemoteIngressError() from None


async def verify_windows_interface(config):
    if config.test_loopback:
        return
    if os.name != 'nt':
        raise RemoteIngressError()
    # Arguments are passed as data, never interpolated into PowerShell code.
    script = Path(__file__).resolve().parents[4] / 'ops/remote_access/windows/inspect-interface.ps1'
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    process = await asyncio.create_subprocess_exec(str(executable), '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'RemoteSigned',  # This child process only; no system policy change.
        '-File', str(script), '-InterfaceAlias', config.interface_alias, '-Address', config.listen_address,
        '-PeerAddress', config.peer_address, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode != 0 or json.loads(stdout.decode('utf-8-sig')).get('verified') is not True:
            raise RemoteIngressError()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


class TransparentBridge:
    def __init__(self, app, ingress, config):
        self.app, self.ingress, self.config = app, ingress, config
        self.listener = None
        self.tasks = set()
        self.writers = set()

    def available(self):
        binding = self.app.state.remote_ingress_binding
        return (self.ingress.config.app is self.app and self.ingress.started and
                not self.ingress.should_exit and binding is not None and
                binding.runtime is not None and binding.runtime.remote_ready)

    async def start(self):
        if self.listener is not None or not self.available():
            raise RemoteIngressError()
        # The destination must be an actual socket of our verified ingress.
        sockets = [sock for server in self.ingress.servers for sock in server.sockets]
        if not sockets or any(sock.getsockname()[:2] != ('127.0.0.1', self.ingress.config.port) for sock in sockets):
            raise RemoteIngressError()
        await self.app.state.remote_ingress_binding.require_ready()
        await verify_windows_interface(self.config)
        self.listener = await asyncio.start_server(self._accept, self.config.listen_address,
            self.config.port, limit=65536, start_serving=True)
        return self.listener.sockets[0].getsockname()[1]

    def _accept(self, reader, writer):
        peer = writer.get_extra_info('peername')
        if (self.listener is None or not self.available() or not peer or peer[0] != self.config.peer_address or
                len(self.tasks) >= self.config.max_connections):
            writer.transport.abort()
            return
        task = asyncio.create_task(self._forward(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _forward(self, reader, writer):
        pumps = []
        upstream = None
        graceful = False
        self.writers.add(writer)
        try:
            async with asyncio.timeout(self.config.lifetime):
                await self.app.state.remote_ingress_binding.require_ready()
                if not self.available():
                    return
                _, target_port = self.ingress.servers[0].sockets[0].getsockname()[:2]
                other, upstream = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', target_port,
                    limit=65536), 5)
                self.writers.add(upstream)
                for stream in (writer, upstream):
                    stream.transport.set_write_buffer_limits(high=65536, low=16384)
                async def pump(source, destination):
                    try:
                        while self.available():
                            async with asyncio.timeout(self.config.idle_timeout):
                                data = await source.read(16384)
                                if not data:
                                    return
                                destination.write(data)
                                await destination.drain()
                    except OSError:
                        return
                    finally:
                        if destination.can_write_eof():
                            try:
                                destination.write_eof()
                            except OSError:
                                pass
                pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(other, writer))]
                done, remaining = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                # Let the reverse direction deliver already queued close/alert
                # records. This is a bounded TCP half-close, never a request retry.
                if remaining:
                    await asyncio.wait(remaining, timeout=min(self.config.idle_timeout, 2))
                graceful = all(not task.cancelled() and task.exception() is None for task in done)
        except Exception:
            pass  # No raw peer/data/exception text in logs.
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            # Remove references before an interruptible graceful-close await.
            for stream in (writer, upstream):
                self.writers.discard(stream)
            for stream in (writer, upstream):
                if stream is not None:
                    if graceful:
                        # Preserve queued TLS records (including WS close frames)
                        # on ordinary EOF. Aborting here can turn 1009 into 1006.
                        stream.close()
                        try:
                            await asyncio.wait_for(stream.wait_closed(), 2)
                        except (OSError, TimeoutError):
                            stream.transport.abort()
                    else:
                        stream.transport.abort()

    async def close(self):
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.close()
        # Modern asyncio Server.wait_closed waits for active connections too.
        # Drain owned tasks/transports FIRST, otherwise active WSS deadlocks stop.
        for stream in tuple(self.writers):
            stream.transport.abort()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.tasks.clear()
        for stream in tuple(self.writers):
            stream.transport.abort()
        self.writers.clear()
        if listener is not None:
            await asyncio.wait_for(listener.wait_closed(), 5)


async def serve_with_bridge(app, ingress, config):
    bridge = TransparentBridge(app, ingress, config)
    task = asyncio.create_task(ingress.serve())
    monitor = None
    try:
        async with asyncio.timeout(30):
            while not ingress.started:
                if task.done():
                    await task
                    raise RemoteIngressError()
                await asyncio.sleep(.02)
        await bridge.start()
        async def check_interface():
            while True:
                await asyncio.sleep(5)
                await verify_windows_interface(config)
                if not bridge.available():
                    raise RemoteIngressError()
        monitor = asyncio.create_task(check_interface())
        done, _ = await asyncio.wait((task, monitor), return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            await completed
    finally:
        await bridge.close()  # Close remote sockets before shutting down ASGI/security.
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        ingress.should_exit = True
        await asyncio.wait_for(task, 10)
