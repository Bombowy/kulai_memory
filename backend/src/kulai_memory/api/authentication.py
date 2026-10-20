"""ASGI enforcement before routing, validation, audio ownership or model work."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from typing import TypeVar

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from kulai_memory.security import (
    Action, Authenticator, AuthorizationPolicy, Principal, SecurityConfig, SecurityError, SecurityMode,
)
from kulai_memory.security.configuration import loopback

logger = logging.getLogger(__name__)
_BEARER = re.compile(rb"Bearer ([A-Za-z0-9_-]{43})", re.IGNORECASE | re.ASCII)
_QUERY_CREDENTIALS = frozenset({"token", "access_token", "authorization", "bearer", "api_key", "secret"})
_MAX_PREAUTH_BYTES = 65536


def _header(scope: Scope, name: bytes) -> bytes | None:
    values = [v for k, v in scope.get("headers", ()) if k.lower() == name]
    if len(values) > 1:
        raise SecurityError()
    return values[0] if values else None


def _uuid(value: bytes | None) -> UUID | None:
    if value is None:
        return None
    try:
        result = UUID(value.decode("ascii"))
        if str(result).encode("ascii") != value:
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise SecurityError() from None


def _action(scope: Scope) -> Action | None:
    path = scope.get("path", "").rstrip("/")
    if scope["type"] == "websocket":
        return {"/ws/memory": Action.MEMORY_WRITE, "/ws/assistant": Action.ASSISTANT_ASK}.get(path)
    method = scope.get("method")
    if method == "GET" and (path == "/api/memories" or re.fullmatch(r"/api/memories/[^/]+", path)):
        return Action.MEMORY_READ
    if method == "GET" and (path == "/api/conversations" or re.fullmatch(r"/api/conversations/[^/]+/messages", path)):
        return Action.MEMORY_READ
    if method == "POST" and (path == "/api/conversations" or re.fullmatch(r"/api/conversations/[^/]+/ask", path)):
        return Action.ASSISTANT_ASK
    if method == "POST":
        return {"/api/assistant/ask": Action.ASSISTANT_ASK,
                "/api/assistant/speech-plan": Action.SPEECH_PLAN}.get(path)
    return None


def _transport_allowed(scope: Scope) -> bool:
    """Only a real numeric loopback listener, never a claimed proxy address."""
    server = scope.get("server")
    if not server or not loopback(server[0]):
        return False
    # Native clients have no Origin. Browser access needs a future explicit policy.
    if _header(scope, b"origin") is not None:
        return False
    if any(k.lower() == b"forwarded" or k.lower().startswith(b"x-forwarded-")
           for k, _ in scope.get("headers", ())):
        return False
    host = _header(scope, b"host")
    if host is None:
        return False
    try:
        authority = urlsplit("//" + host.decode("ascii"))
        if (authority.username or authority.password or authority.path or authority.query or authority.fragment
                or not authority.hostname or authority.port not in (None, server[1])):
            return False
        return authority.hostname == "localhost" or loopback(authority.hostname)
    except (ValueError, UnicodeError):
        return False


def _credentials(scope: Scope) -> tuple[str | None, UUID | None, UUID | None]:
    query = parse_qsl(scope.get("query_string", b"").decode("ascii"),
                      keep_blank_values=True, errors="strict", max_num_fields=20)
    if (scope["type"] == "websocket" and query
            or any(k.casefold() in _QUERY_CREDENTIALS for k, _ in query)
            or _header(scope, b"sec-websocket-protocol") is not None):
        raise SecurityError()
    header = _header(scope, b"authorization")
    device = _uuid(_header(scope, b"x-kulai-device-id"))
    project = _uuid(_header(scope, b"x-kulai-project-id"))
    if header is None:
        if device is not None or project is not None:
            raise SecurityError()
        return None, None, None
    match = _BEARER.fullmatch(header)
    if match is None:
        raise SecurityError()
    return match[1].decode("ascii"), device, project


Result = TypeVar('Result')


class _Disconnected(Exception):
    pass


async def _authenticate_connected(operation: Awaitable[Result], receive: Receive,
                                  buffered: deque[Message], *, timeout: float) -> Result:
    """Single receive owner while storage awaits; replay bounded input unchanged."""
    async def watch() -> None:
        size = 0
        while True:
            message = await receive()
            if message["type"] in ("http.disconnect", "websocket.disconnect"):
                raise _Disconnected()
            size += len(message.get("body", b"")) + len(message.get("bytes", b""))
            size += len(message.get("text", "").encode("utf-8"))
            if size > _MAX_PREAUTH_BYTES or len(buffered) >= 64:
                raise SecurityError()
            buffered.append(message)

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(watch())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                watcher.result()
            return work.result()
    finally:
        work.cancel()
        watcher.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


class AuthenticationMiddleware:
    """SECURE is deny by default. LOCAL_DEV is an explicit transport exception.

    Storage is borrowed; its owner, not this adapter, handles lifecycle. Only a
    Principal reaches scope state. Credentials never enter state or diagnostics.
    """

    def __init__(self, app: ASGIApp, *, config: SecurityConfig,
                 authenticator: Authenticator, policy: AuthorizationPolicy, remote_binding=None) -> None:
        self.app, self.config = app, config
        self.authenticator, self.policy = authenticator, policy
        self.remote_binding = remote_binding

    async def _deny(self, scope: Scope, receive: Receive, send: Send, *, status: int = 401) -> None:
        try:
            if scope["type"] == "websocket":
                # ASGI close before accept produces an HTTP 403 handshake denial.
                await send({"type": "websocket.close", "code": 1008})
            else:
                response = JSONResponse({"detail": "Authentication required." if status == 401 else "Access denied."},
                    status_code=status, headers={"Cache-Control": "no-store", **(
                        {"WWW-Authenticate": "Bearer"} if status == 401 else {})})
                await response(scope, receive, send)
        except OSError:
            pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") == "/health" and scope.get("method") == "GET":
            await self.app(scope, receive, send)
            return
        buffered: deque[Message] = deque()
        try:
            remote = self.remote_binding is not None and self.remote_binding.admitted(scope)
            if (self.remote_binding is not None and not remote or
                    self.remote_binding is None and not _transport_allowed(scope)):
                await self._deny(scope, receive, send, status=403)
                return
            # PL/EN: exact pre-auth exception; no prefix, GET, WS or proxy bypass.
            from .pairing_http import PAIRING_PATHS
            from .session_http import REFRESH_PATH
            if scope['type'] == 'http' and scope.get('method') == 'POST' and scope.get('path') in PAIRING_PATHS | {REFRESH_PATH}:
                peer = scope.get('client')
                if (self.config.mode is not SecurityMode.SECURE or scope.get('query_string')
                        or (not remote and (not peer or not loopback(peer[0])))
                        or _header(scope, b'sec-websocket-protocol') is not None):
                    await self._deny(scope, receive, send, status=403)
                    return
                scope['security_pairing_local'] = True
                async with asyncio.timeout(5):
                    await self.app(scope, receive, send)
                return
            token, device_id, project_id = _credentials(scope)
            # These routes address the existing global canonical Memory. A client
            # header cannot select a project; future adapters must resolve resources.
            if project_id is not None:
                await self._deny(scope, receive, send, status=403)
                return
            action = _action(scope)
            principal = None
            if token is None:
                peer = scope.get("client")
                if (self.config.mode is not SecurityMode.LOCAL_DEV
                        or not peer or not loopback(peer[0])):
                    raise SecurityError()
            else:
                principal = await _authenticate_connected(
                    self.authenticator.authenticate(token, claimed_device_id=device_id), receive,
                    buffered, timeout=self.config.authentication_timeout)
                if not self.policy.permits(principal, action):
                    await self._deny(scope, receive, send, status=403)
                    return
        except _Disconnected:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            # PL/EN: no exception text, token, body, URL, headers or device details.
            await self._deny(scope, receive, send)
            return

        scope.setdefault("state", {})["security_principal"] = principal
        status = None
        accepted = False
        closed = False

        async def authorized_receive() -> Message:
            message = buffered.popleft() if buffered else await receive()
            if token is not None and message["type"] == "websocket.receive":
                try:
                    async with asyncio.timeout(self.config.authentication_timeout):
                        current = await self.authenticator.authenticate(token, claimed_device_id=device_id)
                    if not self.policy.permits(current, action):
                        raise SecurityError()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    try:
                        await send({"type": "websocket.close", "code": 1008})
                    except OSError:
                        pass
                    raise WebSocketDisconnect(code=1008) from None
            return message

        async def safe_send(message: Message) -> None:
            nonlocal status, accepted, closed
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", ()), (b"cache-control", b"no-store")]}
            elif message["type"] == "websocket.accept":
                accepted = True
            elif message["type"] == "websocket.close":
                closed = True
            await send(message)

        try:
            await self.app(scope, authorized_receive, safe_send)
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, OSError):
            return
        except Exception:
            # Safe host fallback for failures outside a route's normal contract.
            # Never propagate an exception payload to Uvicorn traceback logging.
            try:
                if scope["type"] == "http" and status is None:
                    await JSONResponse({"detail": "Operation could not be completed."}, status_code=500)(
                        scope, receive, safe_send)
                elif scope["type"] == "websocket" and not closed:
                    await send({"type": "websocket.close", "code": 1011 if accepted else 1008})
            except (OSError, RuntimeError):
                pass  # Transport already ended; never report payloads through a traceback.
        finally:
            logger.info("private request finished transport=%s action=%s status=%s",
                        scope["type"], action.value if action else "unknown", status)
