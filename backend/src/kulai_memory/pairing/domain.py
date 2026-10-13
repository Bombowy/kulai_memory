"""Validated pairing values; secrets never have a printable representation."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, Callable
from uuid import UUID
import re
import secrets

from kulai_memory.security.models import Action, IssuedCredential, TokenRecord, SecurityError
from kulai_memory.security.authentication import token_digest, Clock

SESSION_TTL = timedelta(minutes=5)
MAX_ACTIVE = 8
MAX_SESSIONS = 128
MAX_ATTEMPTS = 8
REQUESTS_PER_MINUTE = 120


class PairingError(Exception):
    def __init__(self) -> None:
        super().__init__('Pairing could not be completed.')


class PairingState(str, Enum):
    INVITED = 'invited'
    PENDING = 'pending'
    APPROVED = 'approved'
    CLAIMED = 'claimed'
    DENIED = 'denied'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    REVOKED = 'revoked'


def digest(secret: str) -> str:
    try:
        return token_digest(secret)
    except SecurityError:
        raise PairingError() from None


def label(value: str) -> str:
    # PL/EN: a display label, never a shell/log payload or proof of identity.
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9 ._-]{1,48}', value) is None or not value.strip():
        raise PairingError()
    return value.strip()


@dataclass(frozen=True, slots=True)
class Invitation:
    session_id: UUID
    secret: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PairingView:
    session_id: UUID
    state: PairingState
    expires_at: datetime
    device_id: UUID | None
    client_id: UUID | None
    device_label: str | None
    verification_code: str | None


CredentialFactory = Callable[[UUID, frozenset[Action], timedelta], tuple[TokenRecord, IssuedCredential]]


class PairingStore(Protocol):
    async def invite(self, invitation: Invitation, actions: frozenset[Action], token_ttl: timedelta) -> None: ...
    async def request(self, session_id: UUID, invite_digest: str, receiver_digest: str,
                      client_id: UUID, device_label: str) -> PairingView | None: ...
    async def status(self, session_id: UUID, receiver_digest: str, *, cancel: bool = False) -> PairingView | None: ...
    async def decide(self, session_id: UUID, device_id: UUID, *, approve: bool) -> None: ...
    async def claim(self, session_id: UUID, receiver_digest: str, factory: CredentialFactory) -> IssuedCredential | None: ...
    async def pending(self) -> list[PairingView]: ...


class PairingService:
    """Application orchestration. Authorization/transactions belong to the port."""
    def __init__(self, store: PairingStore, *, clock: Clock) -> None:
        self.store, self.clock = store, clock

    async def invite(self, *, actions: frozenset[Action], token_ttl: timedelta = timedelta(hours=1)) -> Invitation:
        from uuid import uuid4
        if (not isinstance(actions, frozenset) or not actions or any(not isinstance(a, Action) for a in actions)
                or not isinstance(token_ttl, timedelta)
                or not timedelta(seconds=1) <= token_ttl <= timedelta(hours=24)):
            raise PairingError()
        invitation = Invitation(uuid4(), secrets.token_urlsafe(32), self.clock() + SESSION_TTL)
        await self.store.invite(invitation, actions, token_ttl)
        return invitation

    async def request(self, session_id: UUID, invitation: str, receiver: str, client_id: UUID, device_label: str) -> PairingView:
        if not isinstance(session_id, UUID) or not isinstance(client_id, UUID):
            raise PairingError()
        result = await self.store.request(session_id, digest(invitation), digest(receiver), client_id, label(device_label))
        if result is None:
            raise PairingError()
        return result

    async def status(self, session_id: UUID, receiver: str, *, cancel: bool = False) -> PairingView:
        result = await self.store.status(session_id, digest(receiver), cancel=cancel)
        if result is None:
            raise PairingError()
        return result

    async def claim(self, session_id: UUID, receiver: str) -> IssuedCredential:
        from kulai_memory.security.authentication import new_credential
        result = await self.store.claim(session_id, digest(receiver),
            lambda device, actions, ttl: new_credential(device, ttl, actions, None, self.clock()))
        if result is None:
            raise PairingError()
        return result
