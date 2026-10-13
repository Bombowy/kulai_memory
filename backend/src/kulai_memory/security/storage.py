"""Security storage ports. Production adapters must provide atomic mutations."""
from typing import Protocol
from datetime import datetime
from uuid import UUID

from .models import CredentialState, Device, DeviceStatus, TokenRecord


class SecurityReader(Protocol):
    async def lookup(self, digest: str) -> CredentialState | None:
        """Read token and device in one consistent snapshot; no raw secrets."""
        ...


class SecurityStore(SecurityReader, Protocol):
    async def register_device(self, device: Device) -> None: ...
    async def set_device_status(self, device_id: UUID, status: DeviceStatus) -> None: ...
    async def create_token(self, token: TokenRecord) -> None:
        """Insert only if the owning device is still trusted, atomically."""
        ...
    async def revoke_token(self, token_id: UUID, *, now: datetime) -> None: ...
    async def rotate_token(self, token_id: UUID, replacement: TokenRecord, *, now: datetime) -> None:
        """Atomically revoke active old token and insert its same-device successor.

        Reject revoked/expired tokens and untrusted devices; preserve permissions
        and project. PostgreSQL implementation will require its own migration.
        """
        ...


class DenyAllSecurityReader:
    """Unconfigured SECURE mode cannot authenticate anyone."""

    async def lookup(self, digest: str) -> None:
        return None
