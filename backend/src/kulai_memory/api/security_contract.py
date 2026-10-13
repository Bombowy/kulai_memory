"""PL/EN: shared bounded secret-bearing JSON parser; no reflected validation errors."""
import json
from uuid import UUID
from fastapi import Request
from kulai_memory.security.models import SecurityError, IssuedCredential
from kulai_memory.security.sessions import IssuedSessionCredential
from .authentication import _header


def canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise SecurityError()
    result = UUID(value)
    if str(result) != value:
        raise SecurityError()
    return result


async def bounded_json(request: Request, keys: set[str]) -> dict:
    media_type = (_header(request.scope, b'content-type') or b'').decode('ascii').lower().replace(' ', '')
    if media_type not in ('application/json', 'application/json;charset=utf-8'):
        raise SecurityError()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise SecurityError()
        body.extend(chunk)
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise SecurityError()
            result[key] = value
        return result
    data = json.loads(body, object_pairs_hook=pairs)
    if not isinstance(data, dict) or set(data) != keys:
        raise SecurityError()
    return data


def credential_payload(value: IssuedCredential) -> dict:
    data = {'device_id': str(value.device_id), 'token_id': str(value.token_id),
            'access_token': value.access_token, 'expires_at': value.expires_at.isoformat()}
    if isinstance(value, IssuedSessionCredential):
        data.update(session_id=str(value.session_id), refresh_id=str(value.refresh_id),
            refresh_credential=value.refresh_credential, refresh_expires_at=value.refresh_expires_at.isoformat(),
            session_expires_at=value.session_expires_at.isoformat())
    return data
