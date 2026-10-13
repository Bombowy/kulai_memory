"""PL: osobna capability odnowienia. EN: no transport, persistence or Memory dependency."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import re
import secrets
from typing import Protocol
from uuid import UUID, uuid4

from .models import IssuedCredential, SecurityError

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
SESSION_TTL = timedelta(days=90)
MAX_ROTATIONS = 4096
REFRESH_PATTERN = re.compile(r"krf1_[A-Za-z0-9_-]{43}", re.ASCII)


def refresh_digest(secret: str) -> str:
    if not isinstance(secret, str) or REFRESH_PATTERN.fullmatch(secret) is None:
        raise SecurityError()
    # PL/EN: standard SHA-256 with a distinct credential namespace, not a password KDF.
    return hashlib.sha256(b"kulai-refresh-v1\0" + secret.encode("ascii")).hexdigest()


def new_refresh() -> tuple[UUID, str, str]:
    secret = "krf1_" + secrets.token_urlsafe(32)
    return uuid4(), secret, refresh_digest(secret)


@dataclass(frozen=True, slots=True)
class IssuedSessionCredential(IssuedCredential):
    session_id: UUID
    refresh_id: UUID
    refresh_credential: str = field(repr=False)
    refresh_expires_at: datetime
    session_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewalResult:
    credential: IssuedSessionCredential | None = field(default=None, repr=False)
    retry_later: bool = False


class SessionStore(Protocol):
    async def renew(self, session_id: UUID, device_id: UUID, refresh_id: UUID,
                    digest: str) -> RenewalResult: ...


class SessionService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def renew(self, session_id: UUID, device_id: UUID, refresh_id: UUID,
                    secret: str) -> RenewalResult:
        if any(not isinstance(value, UUID) for value in (session_id, device_id, refresh_id)):
            raise SecurityError()
        return await self._store.renew(session_id, device_id, refresh_id, refresh_digest(secret))
