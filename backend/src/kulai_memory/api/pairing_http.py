"""Exact pre-auth pairing routes; bounded manual parsing never reflects secrets."""
import asyncio
import re
from dataclasses import asdict
from uuid import UUID
from collections import deque
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse
from starlette.requests import ClientDisconnect
from kulai_memory.pairing.domain import PairingService, PairingError
from kulai_memory.api.authentication import _header, _authenticate_connected, _Disconnected
from .security_contract import bounded_json, canonical_uuid, credential_payload

PAIRING_PATHS = frozenset('/api/pairing/' + operation for operation in ('request', 'status', 'claim', 'cancel'))
_PROOF = re.compile(rb'Pairing ([A-Za-z0-9_-]{43})', re.ASCII)


def create_pairing_router(*, service: PairingService | None = None) -> APIRouter:
    router = APIRouter()

    async def operation(request: Request):
        try:
            if not request.scope.get('security_pairing_local'):
                raise PairingError()
            header = _header(request.scope, b'authorization')
            match = _PROOF.fullmatch(header or b'')
            if not match:
                raise PairingError()
            name = request.url.path.rsplit('/', 1)[1]
            keys = {'session_id', 'receiver_secret', 'client_id', 'device_label'} if name == 'request' else {'session_id'}
            data = await bounded_json(request, keys)
            sid = canonical_uuid(data['session_id'])
            active = service
            if active is None:
                runtime = getattr(request.app.state, 'security_runtime', None)
                active = runtime.pairing_service if runtime is not None and runtime.ready else None
            if active is None:
                return JSONResponse({'detail': 'Pairing unavailable.'}, status_code=503, headers={'Cache-Control': 'no-store'})
            proof = match[1].decode('ascii')
            async def execute():
                if name == 'request':
                    client = canonical_uuid(data['client_id'])
                    return await active.request(sid, proof, data['receiver_secret'], client, data['device_label'])
                elif name == 'claim':
                    return await active.claim(sid, proof)
                else:
                    return await active.status(sid, proof, cancel=name == 'cancel')
            view = await _authenticate_connected(execute(), request.receive, deque(), timeout=5)
            if name == 'claim':
                return JSONResponse(credential_payload(view),
                    headers={'Cache-Control': 'no-store'})
            payload = {key: value.isoformat() if hasattr(value, 'isoformat') else str(value) if isinstance(value, UUID)
                       else value for key, value in asdict(view).items()}
            return JSONResponse(payload, headers={'Cache-Control': 'no-store'})
        except asyncio.CancelledError:
            raise
        except (ClientDisconnect, _Disconnected):
            return JSONResponse({'detail': 'Pairing could not be completed.'}, status_code=400,
                                headers={'Cache-Control': 'no-store'})
        except Exception:
            # PL/EN: no validation details, request fields, SQL or exception text.
            return JSONResponse({'detail': 'Pairing could not be completed.'}, status_code=400,
                                headers={'Cache-Control': 'no-store'})

    for path in sorted(PAIRING_PATHS):
        router.add_api_route(path, operation, methods=['POST'], include_in_schema=False)
    return router
