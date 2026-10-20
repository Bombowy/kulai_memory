"""Capabilities created by the TLS protocol, consumed by the existing ASGI auth."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import re
import ssl
import time
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from kulai_memory.security import SecurityMode
from .configuration import RemoteIngressError

PEER_STATE = "_kulai_tls_peer"
ADMISSION = "_kulai_remote_admission"
_POST = frozenset({"/api/assistant/ask", "/api/assistant/speech-plan", "/api/security/refresh",
                  *["/api/pairing/" + op for op in ("request", "status", "claim", "cancel")]})
_WS = frozenset({"/ws/memory", "/ws/assistant"})


@dataclass(frozen=True, repr=False)
class _Peer:
    owner: object = field(repr=False)
    transport: object = field(repr=False)
    fingerprint: str
    expires: float


@dataclass(frozen=True, repr=False)
class _Admission:
    owner: object
    scope: object
    peer: _Peer


class RemoteBinding:
    def __init__(self, config, security_config):
        self.config = config
        self.security_config = security_config
        self.runtime = None
        self._owner = object()

    def peer_from_transport(self, transport):
        """Only called by Uvicorn connection_made after the OpenSSL handshake."""
        try:
            tls = transport.get_extra_info("ssl_object")
            if (not isinstance(tls, (ssl.SSLObject, ssl.SSLSocket)) or
                    tls.context.verify_mode != ssl.CERT_REQUIRED or
                    tls.version() not in ("TLSv1.2", "TLSv1.3")):
                raise RemoteIngressError()
            certificate = tls.getpeercert()
            if ("DNS", self.config.expected_gateway_identity) not in certificate.get("subjectAltName", ()):
                raise RemoteIngressError()
            fingerprint = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            if self.config.gateway_sha256 is not None and fingerprint != self.config.gateway_sha256:
                raise RemoteIngressError()
            expires = ssl.cert_time_to_seconds(certificate["notAfter"])
            if not ssl.cert_time_to_seconds(certificate["notBefore"]) <= time.time() < expires:
                raise RemoteIngressError()
            return _Peer(self._owner, transport, fingerprint, expires)
        except Exception:
            raise RemoteIngressError() from None

    def valid_peer(self, peer):
        return (isinstance(peer, _Peer) and peer.owner is self._owner and
                time.time() < peer.expires and not peer.transport.is_closing())

    def admitted(self, scope):
        value = scope.get(ADMISSION)
        return (isinstance(value, _Admission) and value.owner is self._owner and value.scope is scope and
                self.valid_peer(value.peer) and self.security_config.mode is SecurityMode.SECURE and
                self.runtime is not None and self.runtime.remote_ready)

    async def require_ready(self):
        if self.security_config.mode is not SecurityMode.SECURE or self.runtime is None:
            raise RemoteIngressError()
        await self.runtime.require_remote_ready()

    def policy(self, scope):
        headers = scope.get("headers", ())
        if len(headers) > 64 or sum(len(k) + len(v) for k, v in headers) > 8192:
            raise RemoteIngressError()
        names = [key.lower() for key, _ in headers]
        if len(set(names)) != len(names):
            raise RemoteIngressError()
        if any(name in (b"origin", b"forwarded", b"x-real-ip", b"sec-websocket-protocol", b"content-encoding") or
               name.startswith((b"x-forwarded-", b"x-verified", b"x-kulai-ingress")) for name in names):
            raise RemoteIngressError()
        host = dict(zip(names, [value for _, value in headers])).get(b"host", b"").decode("ascii")
        expected = urlsplit(self.config.public_origin)
        port = expected.port if expected.port is not None else 443
        authorities = {expected.hostname + ':' + str(port)}
        if port == 443:
            authorities.add(expected.hostname)
        if (host.lower() not in authorities or
                scope.get("scheme") != ("wss" if scope["type"] == "websocket" else "https")):
            raise RemoteIngressError()
        path = scope.get("path", "")
        if scope.get("raw_path") != path.encode("ascii"):
            raise RemoteIngressError()
        if scope["type"] == "websocket":
            allowed = path in _WS
        else:
            method = scope.get("method")
            conversation = re.fullmatch(r"/api/conversations/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/(messages|ask)", path)
            conversation_allowed = (method in ("GET", "POST") and path == "/api/conversations" or
                conversation is not None and (method, conversation[1]) in (("GET", "messages"), ("POST", "ask")))
            allowed = (method == "POST" and path in _POST or method == "GET" and
                       (path == "/api/memories" or re.fullmatch(r"/api/memories/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", path))
                       or conversation_allowed)
        return bool(allowed)


class RemoteIngressGuard:
    """No scope peer/admission can be supplied by a network header or proxy."""
    def __init__(self, app, *, binding):
        self.app, self.binding = app, binding

    async def deny(self, scope, receive, send, status):
        response = JSONResponse({"detail": "Remote access unavailable."}, status_code=status,
                                headers={"Cache-Control": "no-store"})
        if scope["type"] == "http":
            await response(scope, receive, send)
        elif "websocket.http.response" in scope.get("extensions", {}):
            await send({"type": "websocket.http.response.start", "status": status,
                        "headers": list(response.raw_headers)})
            await send({"type": "websocket.http.response.body", "body": response.body})
        else:
            await send({"type": "websocket.close", "code": 1008})

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        peer = scope.get("state", {}).get(PEER_STATE)
        try:
            if not self.binding.valid_peer(peer):
                raise RemoteIngressError()
            allowed = self.binding.policy(scope)
        except Exception:
            await self.deny(scope, receive, send, 403)
            return
        if not allowed:
            await self.deny(scope, receive, send, 404)
            return
        try:
            await self.binding.require_ready()
        except Exception:
            await self.deny(scope, receive, send, 503)
            return
        scope[ADMISSION] = _Admission(self.binding._owner, scope, peer)
        config = self.binding.config
        first = None
        if scope["type"] == "http":
            data = bytearray()
            limit = min(config.max_body_bytes, 4096) if scope["path"].startswith(("/api/pairing/", "/api/security/")) else config.max_body_bytes
            try:
                async with asyncio.timeout(config.body_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        data.extend(message.get("body", b""))
                        if len(data) > limit:
                            await self.deny(scope, receive, send, 413)
                            return
                        if not message.get("more_body", False):
                            break
                first = {"type": "http.request", "body": bytes(data), "more_body": False}
            except TimeoutError:
                await self.deny(scope, receive, send, 408)
                return
        async def guarded_receive():
            nonlocal first
            if first is not None:
                message, first = first, None
            else:
                try:
                    async with asyncio.timeout(config.idle_timeout):
                        message = await receive()
                except TimeoutError:
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1013})
                        raise WebSocketDisconnect(1013) from None
                    raise
            if not self.binding.admitted(scope):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1013})
                    raise WebSocketDisconnect(1013)
                raise RemoteIngressError()
            if len(message.get("bytes", b"")) > config.max_frame_bytes or len(message.get("text", "").encode("utf-8")) > 16384:
                await send({"type": "websocket.close", "code": 1009})
                raise WebSocketDisconnect(1009)
            return message
        accepted = False
        started = False
        async def bounded_send(message):
            nonlocal accepted, started
            accepted |= message["type"] == "websocket.accept"
            started |= message["type"] == "http.response.start"
            async with asyncio.timeout(config.idle_timeout):
                await send(message)
        try:
            async with asyncio.timeout(config.websocket_timeout if scope["type"] == "websocket" else config.http_timeout):
                await self.app(scope, guarded_receive, bounded_send)
        except (TimeoutError, RemoteIngressError):
            if accepted:
                await send({"type": "websocket.close", "code": 1013})
            elif not started:
                await self.deny(scope, receive, send, 503)
        finally:
            scope.pop(ADMISSION, None)
