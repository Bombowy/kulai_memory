"""Narrow refresh-only capability transport; no AI, redirects or transaction retry."""
import asyncio
import re
from collections import deque
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse
from kulai_memory.security.sessions import SessionService
from .authentication import _header, _authenticate_connected
from .security_contract import bounded_json, canonical_uuid, credential_payload

REFRESH_PATH = '/api/security/refresh'
_PROOF = re.compile(rb'Refresh (krf1_[A-Za-z0-9_-]{43})', re.ASCII)


def create_session_router(*, service: SessionService | None = None) -> APIRouter:
    router = APIRouter()

    def failure(status: int, code: str, *, unapplied: bool = False) -> JSONResponse:
        return JSONResponse({'detail': 'Session renewal unavailable.', 'code': code,
            **({'request_applied': False} if unapplied else {})}, status_code=status,
            headers={'Cache-Control': 'no-store', **({'Retry-After': '30'} if status == 429 else {})})

    async def renew(request: Request):
        entered = False
        try:
            if not request.scope.get('security_pairing_local'):
                return failure(401, 'renewal_required')
            match = _PROOF.fullmatch(_header(request.scope, b'authorization') or b'')
            if not match or _header(request.scope, b'x-kulai-device-id') or _header(request.scope, b'x-kulai-project-id'):
                return failure(401, 'renewal_required')
            data = await bounded_json(request, {'session_id', 'device_id', 'refresh_id'})
            ids = [canonical_uuid(data[key]) for key in ('session_id', 'device_id', 'refresh_id')]
            active = service
            if active is None:
                runtime = getattr(request.app.state, 'security_runtime', None)
                active = getattr(runtime, 'session_service', None) if runtime is not None and runtime.ready else None
            if active is None:
                return failure(503, 'unavailable', unapplied=True)
            entered = True
            result = await _authenticate_connected(active.renew(*ids, match[1].decode('ascii')),
                request.receive, deque(), timeout=5)
            if result.retry_later:
                return failure(429, 'retry_later', unapplied=True)
            if result.credential is None:
                return failure(401, 'renewal_required')
            return JSONResponse(credential_payload(result.credential), headers={'Cache-Control': 'no-store'})
        except asyncio.CancelledError:
            raise
        except Exception:
            # PL: nieznany COMMIT nie jest ponawiany. EN: never assert rollback after storage entry.
            return failure(503, 'uncertain') if entered else failure(400, 'invalid_request')

    router.add_api_route(REFRESH_PATH, renew, methods=['POST'], include_in_schema=False)
    return router
