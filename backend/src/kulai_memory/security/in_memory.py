"""Process-only reference/test storage; nothing survives process restart."""
from dataclasses import replace
from datetime import datetime
from threading import RLock
from uuid import UUID

from .models import CredentialState, Device, DeviceStatus, SecurityError, TokenRecord, aware


class InMemorySecurityStore:
    """Borrowed by authentication. No DB, files, providers or background tasks."""

    def __init__(self) -> None:
        self._devices: dict[UUID, Device] = {}
        self._tokens: dict[str, TokenRecord] = {}
        self._digests: dict[UUID, str] = {}
        # PL: chroni też TestClient z różnych wątków. EN: atomic across threads.
        self._lock = RLock()

    async def lookup(self, digest: str) -> CredentialState | None:
        with self._lock:
            token = self._tokens.get(digest)
            device = self._devices.get(token.device_id) if token else None
            return CredentialState(device, token) if device and token else None

    async def register_device(self, device: Device) -> None:
        with self._lock:
            if device.id in self._devices:
                raise SecurityError()
            self._devices[device.id] = device

    async def set_device_status(self, device_id: UUID, status: DeviceStatus) -> None:
        with self._lock:
            device = self._devices.get(device_id)
            if (device is None or device.status is DeviceStatus.REVOKED
                    or not isinstance(status, DeviceStatus)):
                raise SecurityError()
            self._devices[device_id] = replace(device, status=status)

    def _check_insert(self, token: TokenRecord) -> None:
        device = self._devices.get(token.device_id)
        if (device is None or device.status is not DeviceStatus.TRUSTED
                or token.digest in self._tokens or token.id in self._digests
                or token.revoked_at is not None):
            raise SecurityError()

    def _insert(self, token: TokenRecord) -> None:
        self._tokens[token.digest] = token
        self._digests[token.id] = token.digest

    async def create_token(self, token: TokenRecord) -> None:
        with self._lock:
            self._check_insert(token)
            self._insert(token)

    async def revoke_token(self, token_id: UUID, *, now: datetime) -> None:
        aware(now)
        with self._lock:
            digest = self._digests.get(token_id)
            if digest is None:
                raise SecurityError()
            token = self._tokens[digest]
            if token.revoked_at is None:
                self._tokens[digest] = replace(token, revoked_at=now)

    async def rotate_token(self, token_id: UUID, replacement: TokenRecord, *, now: datetime) -> None:
        with self._lock:
            digest = self._digests.get(token_id)
            token = self._tokens.get(digest) if digest else None
            if (token is None or not token.active(now)
                    or replacement.device_id != token.device_id
                    or replacement.actions != token.actions or replacement.project_id != token.project_id
                    or replacement.issued_at != now):
                raise SecurityError()
            self._check_insert(replacement)
            self._tokens[digest] = replace(token, revoked_at=now)
            self._insert(replacement)
