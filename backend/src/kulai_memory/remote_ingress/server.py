"""Uvicorn owns HTTP/WS/TLS parsing; we bind its real TLS transport to ASGI."""
import logging
import ssl
from importlib.metadata import version
from ipaddress import ip_address

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

from .boundary import PEER_STATE
from .configuration import RemoteIngressError


def create_remote_server(app, *, host="127.0.0.1", port=8443):
    """This task's launcher deliberately permits numeric loopback only."""
    try:
        if not ip_address(host).is_loopback or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError()
        if version("uvicorn") != "0.54.0":
            raise ValueError()
        binding = app.state.remote_ingress_binding
        if binding is None or not binding.config.enabled:
            raise ValueError()
        context = binding.config.tls_context()  # Check paths/key/CA before startup/listen.
    except Exception:
        raise RemoteIngressError() from None

    def bind(protocol, transport):
        protocol.app_state = protocol.app_state.copy()
        if len(protocol.connections) > 64:
            transport.abort()
            return
        try:
            protocol.app_state[PEER_STATE] = binding.peer_from_transport(transport)
        except RemoteIngressError:
            transport.close()

    class Http(H11Protocol):
        def connection_made(self, transport):
            super().connection_made(transport)
            bind(self, transport)

    class WebSocket(WebSocketsSansIOProtocol):
        def connection_made(self, transport):
            super().connection_made(transport)
            bind(self, transport)

        def send_receive_event_to_app(self):
            # Uvicorn 0.54 calls this for text, binary AND reassembled fragments.
            # Pausing reads alone cannot stop the remaining events parsed from
            # the current TCP chunk. Bound queued data before the real enqueue.
            if getattr(self, '_burst_aborted', False) or self.transport.is_closing():
                self.frames = []
                return
            if self.queue.qsize() >= 8:
                self._burst_aborted = True
                self.frames = []
                self.transport.abort()
                return
            super().send_receive_event_to_app()

    settings = uvicorn.Config(app, host=host, port=port, http=Http, ws=WebSocket,
        lifespan="on", proxy_headers=False, access_log=False, log_config=None,
        log_level="critical", server_header=False, workers=1, reload=False,
        ssl_certfile=str(binding.config.certificate), ssl_keyfile=str(binding.config.private_key),
        ssl_cert_reqs=ssl.CERT_REQUIRED, ssl_ca_certs=str(binding.config.client_ca),
        ssl_context_factory=lambda *_: context,
        ws_max_size=binding.config.max_frame_bytes, ws_max_queue=8,
        ws_per_message_deflate=False, ws_ping_interval=30, ws_ping_timeout=30,
        h11_max_incomplete_event_size=8192, limit_concurrency=64,
        timeout_keep_alive=5, timeout_graceful_shutdown=5)
    # Uvicorn's default request/error diagnostics can include raw targets/frames.
    # Only our fixed category diagnostics are suitable for the private listener.
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            try:
                await super().startup(sockets=sockets)
            except SystemExit:
                # Library startup refusal must not kill a hosting asyncio loop.
                self.should_exit = True
                raise RemoteIngressError() from None

    return Server(settings)
