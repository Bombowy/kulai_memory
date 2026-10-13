"""Opaque 256-bit bearer credentials; standard CSPRNG and SHA-256 only."""
from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .models import (
    Action, Device, DeviceStatus, IssuedCredential, Principal, SecurityError, TokenRecord, aware,
)
from .storage import SecurityReader, SecurityStore

Clock = Callable[[], datetime]
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def token_digest(token: str) -> str:
    if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
        raise SecurityError()
    # PL: wysoka entropia sekretu; SHA-256 nie jest hashowaniem hasła użytkownika.
    # EN: random 256-bit secret, not a human password; store no plaintext.
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_credential(device_id: UUID, ttl: timedelta, actions: frozenset[Action],
                   project_id: UUID | None, now: datetime) -> tuple[TokenRecord, IssuedCredential]:
    """PL/EN: pure issuance preparation; the caller owns atomic persistence."""
    aware(now)
    if not isinstance(ttl, timedelta) or not timedelta(0) < ttl <= timedelta(days=30):
        raise SecurityError()
    secret = secrets.token_urlsafe(32)
    record = TokenRecord(uuid4(), device_id, token_digest(secret), now, now + ttl, actions, project_id)
    return record, IssuedCredential(device_id, record.id, secret, record.expires_at)


class Authenticator:
    def __init__(self, reader: SecurityReader, *, clock: Clock = utc_now) -> None:
        self._reader = reader
        self._clock = clock

    async def authenticate(self, token: str, *, claimed_device_id: UUID | None = None) -> Principal:
        try:
            digest = token_digest(token)
            state = await self._reader.lookup(digest)
            if (state is None or not secrets.compare_digest(state.token.digest, digest)
                    or state.device.status is not DeviceStatus.TRUSTED
                    or state.device.id != state.token.device_id or not state.token.active(self._clock())
                    or claimed_device_id is not None and claimed_device_id != state.device.id):
                raise SecurityError()
            return Principal(state.device.id, state.token.id, state.token.expires_at,
                             state.token.actions, state.token.project_id)
        except Exception:
            raise SecurityError() from None


class CredentialManager:
    """Trusted provisioning API, never an unauthenticated network endpoint."""

    def __init__(self, store: SecurityStore, *, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    async def register_device(self, *, trusted: bool = False) -> Device:
        if type(trusted) is not bool:
            raise SecurityError()
        device = Device(uuid4(), DeviceStatus.TRUSTED if trusted else DeviceStatus.PENDING, self._clock())
        await self._store.register_device(device)
        return device

    async def trust_device(self, device_id: UUID) -> None:
        await self._store.set_device_status(device_id, DeviceStatus.TRUSTED)

    async def revoke_device(self, device_id: UUID) -> None:
        await self._store.set_device_status(device_id, DeviceStatus.REVOKED)

    def _new(self, device_id: UUID, ttl: timedelta, actions: frozenset[Action],
             project_id: UUID | None, now: datetime) -> tuple[TokenRecord, IssuedCredential]:
        return new_credential(device_id, ttl, actions, project_id, now)

    async def issue(self, device_id: UUID, *, ttl: timedelta = timedelta(hours=1),
                    actions: frozenset[Action] = frozenset(Action), project_id: UUID | None = None) -> IssuedCredential:
        record, credential = self._new(device_id, ttl, actions, project_id, self._clock())
        await self._store.create_token(record)
        return credential

    async def revoke(self, token_id: UUID) -> None:
        await self._store.revoke_token(token_id, now=self._clock())

    async def rotate(self, token: str, *, device_id: UUID,
                     ttl: timedelta = timedelta(hours=1)) -> IssuedCredential:
        principal = await Authenticator(self._store, clock=self._clock).authenticate(
            token, claimed_device_id=device_id)
        now = self._clock()
        record, credential = self._new(device_id, ttl, principal.actions, principal.project_id, now)
        await self._store.rotate_token(principal.token_id, record, now=now)
        return credential
