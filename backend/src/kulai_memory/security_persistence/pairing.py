"""Pairing transactions borrow the security pool; no AI/data-domain dependencies."""
from datetime import datetime, timedelta
from collections.abc import Mapping
from typing import Any
import secrets
from uuid import UUID, uuid4
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from kulai_memory.pairing.domain import (
    Invitation, PairingView, PairingState, PairingError, CredentialFactory,
    MAX_ACTIVE, MAX_SESSIONS, MAX_ATTEMPTS, REQUESTS_PER_MINUTE, digest,
)
from kulai_memory.security.models import Action, DeviceStatus, IssuedCredential
from .postgres import PostgresSecurityStore, _values, _token
from .schema_v1 import devices, tokens
from .schema_v2 import pairings, rate


class PostgresPairingStore(PostgresSecurityStore):
    """Lock order: rate advisory -> session -> device -> token.

    PL: błędny dowód zapisuje próbę przed odmową. EN: denial is returned AFTER
    commit so rate/attempt counters cannot be rolled back by an invalid proof.
    """
    async def _now(self, session: AsyncSession) -> datetime:
        return await session.scalar(sa.select(sa.func.clock_timestamp()))

    async def _rate(self, session: AsyncSession, now: datetime) -> bool:
        await session.execute(sa.text('SELECT pg_advisory_xact_lock(49142002)'))
        row = (await session.execute(sa.select(rate).where(rate.c.id == 1).with_for_update())).mappings().one_or_none()
        if row is None:
            await session.execute(sa.insert(rate).values(id=1, window_start=now, attempts=1))
            return True
        reset = now >= row['window_start'] + timedelta(minutes=1)
        if not reset and row['attempts'] >= REQUESTS_PER_MINUTE:
            return False
        await session.execute(sa.update(rate).where(rate.c.id == 1).values(
            window_start=now if reset else row['window_start'], attempts=1 if reset else row['attempts'] + 1))
        return True

    async def _session(self, session: AsyncSession, session_id: UUID) -> Mapping[str, Any] | None:
        return (await session.execute(sa.select(pairings).where(pairings.c.id == session_id).with_for_update())).mappings().one_or_none()

    async def _proof(self, session: AsyncSession, row: Mapping[str, Any] | None, candidate: str, *, invitation: bool = False) -> bool:
        if row is None or row['attempts'] >= MAX_ATTEMPTS:
            return False
        expected = row['invite_digest' if invitation else 'receiver_digest']
        if expected is not None and secrets.compare_digest(expected, candidate):
            return True
        await session.execute(sa.update(pairings).where(pairings.c.id == row['id']).values(attempts=row['attempts'] + 1))
        return False

    def _view(self, row: Mapping[str, Any], now: datetime, device_status: str | None = None) -> PairingView:
        state = PairingState(row['state'])
        if device_status == DeviceStatus.REVOKED.value and state not in (PairingState.CANCELLED, PairingState.DENIED):
            state = PairingState.REVOKED
        elif now >= row['expires_at'] and state not in (PairingState.CLAIMED, PairingState.DENIED, PairingState.CANCELLED):
            state = PairingState.EXPIRED
        return PairingView(row['id'], state, row['expires_at'], row['device_id'],
            row['client_id'], row['device_label'], row['receiver_digest'])

    async def invite(self, invitation: Invitation, actions: frozenset[Action], token_ttl: timedelta) -> None:
        async with self._transaction() as session:
            await session.execute(sa.text('SELECT pg_advisory_xact_lock(49142002)'))
            now = await self._now(session)
            total = await session.scalar(sa.select(sa.func.count()).select_from(pairings))
            active = await session.scalar(sa.select(sa.func.count()).select_from(pairings).where(
                pairings.c.expires_at > now, pairings.c.state.in_(['invited', 'pending', 'approved'])))
            if total >= MAX_SESSIONS or active >= MAX_ACTIVE or not now < invitation.expires_at <= now + timedelta(minutes=5):
                raise PairingError()
            await session.execute(sa.insert(pairings).values(id=invitation.session_id,
                invite_digest=digest(invitation.secret), state='invited', created_at=now,
                expires_at=invitation.expires_at, actions=sorted(a.value for a in actions),
                token_ttl_seconds=int(token_ttl.total_seconds()), attempts=0))

    async def request(self, session_id: UUID, invite_digest: str, receiver_digest: str,
                      client_id: UUID, device_label: str) -> PairingView | None:
        async with self._transaction() as session:
            now = await self._now(session)
            if not await self._rate(session, now):
                return None
            row = await self._session(session, session_id)
            now = await self._now(session)
            if not await self._proof(session, row, invite_digest, invitation=True) or now >= row['expires_at']:
                return None
            if row['state'] != 'invited':
                # Registration retry proves BOTH invitation and the bound receiver.
                if not await self._proof(session, row, receiver_digest):
                    return None
                if (row['state'] in ('pending', 'approved')
                        and row['client_id'] == client_id and row['device_label'] == device_label):
                    return self._view(row, now)
                return None
            device_id = uuid4()
            await session.execute(sa.insert(devices).values(id=device_id, status='pending', created_at=now))
            await session.execute(sa.update(pairings).where(pairings.c.id == session_id).values(
                receiver_digest=receiver_digest, client_id=client_id, device_id=device_id,
                device_label=device_label, state='pending'))
            row = await self._session(session, session_id)
            now = await self._now(session)
            return self._view(row, now)

    async def status(self, session_id: UUID, receiver_digest: str, *, cancel: bool = False) -> PairingView | None:
        async with self._transaction() as session:
            now = await self._now(session)
            if not await self._rate(session, now):
                return None
            row = await self._session(session, session_id)
            if not await self._proof(session, row, receiver_digest):
                return None
            device = await self._lock_device(session, row['device_id'])
            if cancel and row['state'] in ('pending', 'approved'):
                await session.execute(sa.update(devices).where(devices.c.id == device.id).values(status='revoked'))
                await session.execute(sa.update(pairings).where(pairings.c.id == session_id).values(state='cancelled', approved_at=None))
                row = await self._session(session, session_id)
                return self._view(row, now)
            if cancel and row['state'] == 'claimed':
                await session.execute(sa.update(devices).where(devices.c.id == device.id).values(status='revoked'))
                return self._view(row, now, 'revoked')
            view = self._view(row, now, device.status.value)
            if row['state'] == 'claimed' and device.status is DeviceStatus.TRUSTED:
                record = (await session.execute(sa.select(tokens).where(tokens.c.id == row['token_id']))).mappings().one()
                if not _token(record).active(await self._now(session)):
                    from dataclasses import replace
                    view = replace(view, state=PairingState.REVOKED if record['revoked_at'] is not None else PairingState.EXPIRED)
            return view

    async def decide(self, session_id: UUID, device_id: UUID, *, approve: bool) -> None:
        async with self._transaction() as session:
            now = await self._now(session)
            row = await self._session(session, session_id)
            now = await self._now(session)
            if row is None or row['device_id'] != device_id or row['attempts'] >= MAX_ATTEMPTS or now >= row['expires_at']:
                raise PairingError()
            device = await self._lock_device(session, device_id)
            now = await self._now(session)
            if now >= row['expires_at']:
                raise PairingError()
            if approve and row['state'] == 'approved' and device.status is DeviceStatus.TRUSTED:
                return  # Same explicit session/device approval is idempotent.
            if not approve and row['state'] == 'denied':
                return
            if row['state'] not in ('pending', 'approved') or device.status is DeviceStatus.REVOKED:
                raise PairingError()
            await session.execute(sa.update(devices).where(devices.c.id == device_id).values(status='trusted' if approve else 'revoked'))
            await session.execute(sa.update(pairings).where(pairings.c.id == session_id).values(
                state='approved' if approve else 'denied', approved_at=now if approve else None))

    async def claim(self, session_id: UUID, receiver_digest: str, factory: CredentialFactory) -> IssuedCredential | None:
        async with self._transaction() as session:
            now = await self._now(session)
            if not await self._rate(session, now):
                return None
            row = await self._session(session, session_id)
            now = await self._now(session)
            if not await self._proof(session, row, receiver_digest) or now >= row['expires_at'] or row['state'] != 'approved':
                return None
            await self._lock_device(session, row['device_id'], trusted=True)
            if await self._now(session) >= row['expires_at']:
                return None
            record, credential = factory(row['device_id'], frozenset(Action(a) for a in row['actions']), timedelta(seconds=row['token_ttl_seconds']))
            if (record.device_id != row['device_id'] or record.actions != frozenset(Action(a) for a in row['actions'])
                    or record.project_id is not None or record.revoked_at is not None
                    or record.expires_at - record.issued_at != timedelta(seconds=row['token_ttl_seconds'])
                    or credential.device_id != record.device_id or credential.token_id != record.id
                    or credential.expires_at != record.expires_at or digest(credential.access_token) != record.digest):
                raise PairingError()
            await session.execute(sa.insert(tokens).values(**_values(record)))
            await session.execute(sa.update(pairings).where(pairings.c.id == session_id).values(state='claimed', token_id=record.id))
            return credential

    async def pending(self) -> list[PairingView]:
        async with self._transaction(readonly=True) as session:
            now = await self._now(session)
            rows = (await session.execute(sa.select(pairings).where(pairings.c.state.in_(['pending', 'approved']),
                pairings.c.expires_at > now).order_by(pairings.c.created_at))).mappings()
            return [self._view(row, now) for row in rows]

    async def trusted_devices(self) -> list[UUID]:
        async with self._transaction(readonly=True) as session:
            return list((await session.execute(sa.select(devices.c.id).where(devices.c.status == 'trusted').order_by(devices.c.id))).scalars())

    async def prune_expired(self) -> int:
        """Explicit admin retention operation; never remove a live receiver capability."""
        async with self._transaction() as session:
            await session.execute(sa.text('SELECT pg_advisory_xact_lock(49142002)'))
            now = await self._now(session)
            rows = list((await session.execute(sa.select(pairings).where(pairings.c.expires_at <= now)
                .order_by(pairings.c.id).with_for_update())).mappings())
            removed = 0
            for row in rows:
                if row['device_id'] is not None:
                    device = await self._lock_device(session, row['device_id'])
                    if row['token_id'] is not None:
                        token = (await session.execute(sa.select(tokens).where(tokens.c.id == row['token_id']).with_for_update())).mappings().one()
                        if device.status is DeviceStatus.TRUSTED and _token(token).active(now):
                            continue
                    elif device.status is not DeviceStatus.REVOKED:
                        await session.execute(sa.update(devices).where(devices.c.id == device.id).values(status='revoked'))
                await session.execute(sa.delete(pairings).where(pairings.c.id == row['id']))
                removed += 1
            return removed
