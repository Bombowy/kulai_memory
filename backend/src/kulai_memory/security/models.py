"""Transport-independent security identities and validated immutable records."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID


class SecurityError(Exception):
    """One safe error for invalid credentials, state and storage failures."""

    def __init__(self) -> None:
        super().__init__("Authentication required.")


class DeviceStatus(str, Enum):
    PENDING = "pending"
    TRUSTED = "trusted"
    REVOKED = "revoked"


class Action(str, Enum):
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    ASSISTANT_ASK = "assistant.ask"
    SPEECH_PLAN = "assistant.speech_plan"


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise SecurityError()
    return value


@dataclass(frozen=True, slots=True)
class Device:
    id: UUID
    status: DeviceStatus
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.status, DeviceStatus):
            raise SecurityError()
        aware(self.created_at)


@dataclass(frozen=True, slots=True)
class TokenRecord:
    id: UUID
    device_id: UUID
    digest: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    actions: frozenset[Action]
    project_id: UUID | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.id, UUID) or not isinstance(self.device_id, UUID)
                or self.project_id is not None and not isinstance(self.project_id, UUID)
                or not isinstance(self.digest, str) or len(self.digest) != 64
                or any(c not in "0123456789abcdef" for c in self.digest)
                or not isinstance(self.actions, frozenset)
                or not self.actions or any(not isinstance(a, Action) for a in self.actions)):
            raise SecurityError()
        if aware(self.expires_at) <= aware(self.issued_at):
            raise SecurityError()
        if self.revoked_at is not None:
            aware(self.revoked_at)

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.issued_at <= aware(now) < self.expires_at


@dataclass(frozen=True, slots=True)
class CredentialState:
    device: Device
    token: TokenRecord


@dataclass(frozen=True, slots=True)
class Principal:
    device_id: UUID
    token_id: UUID
    expires_at: datetime
    actions: frozenset[Action]
    project_id: UUID | None


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """Secret returned once to the provisioning caller; never a loggable string."""

    device_id: UUID
    token_id: UUID
    access_token: str = field(repr=False)
    expires_at: datetime
