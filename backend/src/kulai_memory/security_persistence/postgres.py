"""Short PostgreSQL transactions with one cross-process device -> token lock order."""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from kulai_memory.security.models import (
    Action, CredentialState, Device, DeviceStatus, SecurityError, TokenRecord, aware,
)
from .schema_v1 import devices, tokens

SessionFactory = Callable[[], AsyncSession]


def _device(row: Any) -> Device:
    return Device(row["id"], DeviceStatus(row["status"]), row["created_at"])


def _token(row: Any) -> TokenRecord:
    return TokenRecord(row["id"], row["device_id"], row["digest"], row["issued_at"],
        row["expires_at"], frozenset(Action(a) for a in row["actions"]), row["project_id"], row["revoked_at"])


def _values(record: TokenRecord) -> dict[str, Any]:
    return dict(id=record.id, device_id=record.device_id, digest=record.digest,
                issued_at=record.issued_at, expires_at=record.expires_at, revoked_at=record.revoked_at,
                actions=sorted(a.value for a in record.actions), project_id=record.project_id)


class PostgresSecurityStore:
    """Borrow session factory; commit/rollback/close all occur before returning.

    No retries after ambiguous commit failures, no caches or background work.
    PL: blokada urządzenia zawsze poprzedza blokadę tokenu. EN: one lock order.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sessions = session_factory

    @asynccontextmanager
    async def _transaction(self, *, readonly: bool = False) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    if readonly:
                        await session.execute(sa.text("SET TRANSACTION READ ONLY"))
                    yield session
        except Exception:
            # Never propagate SQLAlchemy/driver exceptions containing parameters.
            raise SecurityError() from None

    async def lookup(self, digest: str) -> CredentialState | None:
        async with self._transaction(readonly=True) as session:
            statement = sa.select(tokens, devices.c.id.label("owner_id"), devices.c.status,
                devices.c.created_at).join(devices, tokens.c.device_id == devices.c.id).where(tokens.c.digest == digest)
            row = (await session.execute(statement)).mappings().one_or_none()
            if row is None:
                return None
            return CredentialState(Device(row["owner_id"], DeviceStatus(row["status"]), row["created_at"]), _token(row))

    async def register_device(self, device: Device) -> None:
        async with self._transaction() as session:
            await session.execute(sa.insert(devices).values(id=device.id, status=device.status.value, created_at=device.created_at))

    async def _lock_device(self, session: AsyncSession, device_id: UUID, *, trusted: bool = False) -> Device:
        row = (await session.execute(sa.select(devices).where(devices.c.id == device_id).with_for_update())).mappings().one_or_none()
        if row is None:
            raise SecurityError()
        device = _device(row)
        if trusted and device.status is not DeviceStatus.TRUSTED:
            raise SecurityError()
        return device

    async def set_device_status(self, device_id: UUID, status: DeviceStatus) -> None:
        async with self._transaction() as session:
            device = await self._lock_device(session, device_id)
            if device.status is DeviceStatus.REVOKED or not isinstance(status, DeviceStatus):
                raise SecurityError()
            await session.execute(sa.update(devices).where(devices.c.id == device_id).values(status=status.value))

    async def create_token(self, token: TokenRecord) -> None:
        async with self._transaction() as session:
            await self._lock_device(session, token.device_id, trusted=True)
            if token.revoked_at is not None:
                raise SecurityError()
            await session.execute(sa.insert(tokens).values(**_values(token)))

    async def _lock_token(self, session: AsyncSession, token_id: UUID, *, trusted: bool = False) -> TokenRecord:
        owner = await session.scalar(sa.select(tokens.c.device_id).where(tokens.c.id == token_id))
        if owner is None:
            raise SecurityError()
        await self._lock_device(session, owner, trusted=trusted)
        row = (await session.execute(sa.select(tokens).where(tokens.c.id == token_id).with_for_update())).mappings().one_or_none()
        if row is None:
            raise SecurityError()
        record = _token(row)
        if record.device_id != owner:
            raise SecurityError()  # Never authorize under a lock for a different owner.
        return record

    async def revoke_token(self, token_id: UUID, *, now: datetime) -> None:
        aware(now)
        async with self._transaction() as session:
            record = await self._lock_token(session, token_id)
            if record.revoked_at is None:
                await session.execute(sa.update(tokens).where(tokens.c.id == token_id).values(revoked_at=now))

    async def rotate_token(self, token_id: UUID, replacement: TokenRecord, *, now: datetime) -> None:
        async with self._transaction() as session:
            original = await self._lock_token(session, token_id, trusted=True)
            if (not original.active(now) or replacement.device_id != original.device_id
                    or replacement.actions != original.actions or replacement.project_id != original.project_id
                    or replacement.issued_at != now or replacement.revoked_at is not None):
                raise SecurityError()
            await session.execute(sa.update(tokens).where(tokens.c.id == token_id).values(revoked_at=now))
            await session.execute(sa.insert(tokens).values(**_values(replacement)))
