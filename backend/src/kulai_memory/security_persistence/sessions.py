"""Cross-process renewal: rate -> device -> session -> refresh -> access locks.

PL: odmowa replay zatwierdza revocation. EN: return denial after commit, not raise
inside the transaction. No retries, no plaintext persistence, no authorization cache.
"""
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
import secrets
from typing import Any
from uuid import UUID, uuid4
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from kulai_memory.pairing.domain import PairingView, PairingState
from kulai_memory.security.authentication import new_credential
from kulai_memory.security.models import (
    Action, CredentialState, Device, DeviceStatus, IssuedCredential, SecurityError, TokenRecord,
)
from kulai_memory.security.sessions import (
    ACCESS_TTL, REFRESH_TTL, SESSION_TTL, MAX_ROTATIONS,
    IssuedSessionCredential, RenewalResult, new_refresh,
)
from .pairing import PostgresPairingStore
from .postgres import _token, _values
from .schema_v1 import devices, tokens
from .schema_v3 import sessions, refreshes


class PostgresSessionStore(PostgresPairingStore):
    async def _allow_token_rotation(self, session: AsyncSession, original: TokenRecord) -> None:
        if await session.scalar(sa.select(refreshes.c.id).where(refreshes.c.access_id == original.id)) is not None:
            raise SecurityError() # Never detach a family access from its absolute expiry/revocation.

    def _claim_ttl(self, row: Mapping[str, Any]) -> timedelta:
        return min(super()._claim_ttl(row), ACCESS_TTL)

    async def _after_claim(self, session: AsyncSession, row: Mapping[str, Any],
                           record: TokenRecord, credential: IssuedCredential) -> IssuedSessionCredential:
        sid = uuid4()
        absolute = record.issued_at + SESSION_TTL
        await session.execute(sa.insert(sessions).values(id=sid, device_id=record.device_id,
            pairing_id=row['id'], created_at=record.issued_at, absolute_expires_at=absolute,
            revoked_at=None, actions=sorted(a.value for a in record.actions),
            access_ttl_seconds=int((record.expires_at-record.issued_at).total_seconds()), generation=0))
        return await self._refresh_for_access(session, sid, absolute, credential, record.issued_at)

    async def _refresh_for_access(self, session: AsyncSession, sid: UUID, absolute: datetime,
                                  credential: IssuedCredential, now: datetime) -> IssuedSessionCredential:
        rid, secret, digest = new_refresh()
        expiry = min(now + REFRESH_TTL, absolute)
        await session.execute(sa.insert(refreshes).values(id=rid, session_id=sid,
            access_id=credential.token_id, digest=digest, issued_at=now, expires_at=expiry, consumed_at=None))
        return IssuedSessionCredential(credential.device_id, credential.token_id, credential.access_token,
            credential.expires_at, sid, rid, secret, expiry, absolute)

    async def lookup(self, digest: str) -> CredentialState | None:
        async with self._transaction(readonly=True) as session:
            statement = sa.select(tokens, devices.c.id.label('owner_id'), devices.c.status,
                devices.c.created_at, sessions.c.id.label('session_id'), sessions.c.device_id.label('session_device'),
                sessions.c.revoked_at.label('session_revoked'), sessions.c.absolute_expires_at,
                refreshes.c.consumed_at, sa.func.clock_timestamp().label('now')).select_from(
                    tokens.join(devices, tokens.c.device_id == devices.c.id)
                    .outerjoin(refreshes, refreshes.c.access_id == tokens.c.id)
                    .outerjoin(sessions, refreshes.c.session_id == sessions.c.id)).where(tokens.c.digest == digest)
            row = (await session.execute(statement)).mappings().one_or_none()
            if row is None:
                return None
            if row['session_id'] is not None and (row['session_device'] != row['owner_id']
                    or row['session_revoked'] is not None or row['consumed_at'] is not None
                    or row['now'] >= row['absolute_expires_at']):
                return None
            return CredentialState(Device(row['owner_id'], DeviceStatus(row['status']), row['created_at']), _token(row))

    async def _revoke_family(self, session: AsyncSession, sid: UUID, now: datetime) -> None:
        await session.execute(sa.update(sessions).where(sessions.c.id == sid, sessions.c.revoked_at.is_(None)).values(revoked_at=now))
        await session.execute(sa.update(tokens).where(tokens.c.id.in_(
            sa.select(refreshes.c.access_id).where(refreshes.c.session_id == sid)), tokens.c.revoked_at.is_(None)).values(revoked_at=now))

    async def renew(self, session_id: UUID, device_id: UUID, refresh_id: UUID, digest: str) -> RenewalResult:
        async with self._transaction() as session:
            now = await self._now(session)
            if not await self._rate(session, now):
                return RenewalResult(retry_later=True)
            # Read identity before locking; a bad public claim cannot lock another device.
            owner = await session.scalar(sa.select(sessions.c.device_id).where(sessions.c.id == session_id))
            if owner != device_id:
                return RenewalResult()
            device = await self._lock_device(session, device_id)
            family = (await session.execute(sa.select(sessions).where(sessions.c.id == session_id).with_for_update())).mappings().one()
            old = (await session.execute(sa.select(refreshes).where(refreshes.c.id == refresh_id,
                refreshes.c.session_id == session_id).with_for_update())).mappings().one_or_none()
            now = await self._now(session)
            if old is None or not secrets.compare_digest(old['digest'], digest):
                return RenewalResult()
            if old['consumed_at'] is not None:
                await self._revoke_family(session, session_id, now)
                return RenewalResult()
            if (device.status is not DeviceStatus.TRUSTED or family['revoked_at'] is not None
                    or not old['issued_at'] <= now < old['expires_at']
                    or now >= family['absolute_expires_at'] or family['generation'] >= MAX_ROTATIONS):
                return RenewalResult()
            # Bounded issuance rate even with a valid stolen refresh. Retain short test TTLs.
            if now < old['issued_at'] + timedelta(seconds=min(30, family['access_ttl_seconds'] / 2)):
                return RenewalResult(retry_later=True)
            prior = (await session.execute(sa.select(tokens).where(tokens.c.id == old['access_id']).with_for_update())).mappings().one()
            actions = frozenset(Action(a) for a in family['actions'])
            if (prior['device_id'] != device_id or prior['revoked_at'] is not None
                    or frozenset(prior['actions']) != frozenset(family['actions']) or prior['project_id'] is not None):
                return RenewalResult()
            ttl = min(timedelta(seconds=family['access_ttl_seconds']), family['absolute_expires_at'] - now)
            record, credential = new_credential(device_id, ttl, actions, None, now)
            await session.execute(sa.update(refreshes).where(refreshes.c.id == refresh_id).values(consumed_at=now))
            await session.execute(sa.update(tokens).where(tokens.c.id == old['access_id']).values(revoked_at=now))
            await session.execute(sa.insert(tokens).values(**_values(record)))
            await session.execute(sa.update(sessions).where(sessions.c.id == session_id).values(generation=family['generation']+1))
            issued = await self._refresh_for_access(session, session_id, family['absolute_expires_at'], credential, now)
            return RenewalResult(issued)

    async def _claimed_view(self, session: AsyncSession, row: Mapping[str, Any], view: PairingView) -> PairingView:
        family = (await session.execute(sa.select(sessions).where(sessions.c.pairing_id == row['id']))).mappings().one_or_none()
        if family is None:
            return await super()._claimed_view(session, row, view)  # Historical v2 pairing.
        now = await self._now(session)
        if family['revoked_at'] is not None:
            return replace(view, state=PairingState.REVOKED)
        current = (await session.execute(sa.select(refreshes, tokens.c.revoked_at).join(
            tokens, refreshes.c.access_id == tokens.c.id).where(refreshes.c.session_id == family['id'],
            refreshes.c.consumed_at.is_(None)))).mappings().one_or_none()
        if current is None or current['revoked_at'] is not None:
            return replace(view, state=PairingState.REVOKED)
        if now >= family['absolute_expires_at'] or now >= current['expires_at']:
            return replace(view, state=PairingState.EXPIRED)
        # PL/EN: access expiry is renewable; status must not inspect the initial access.
        return view

    async def _retain_pairing(self, session: AsyncSession, row: Mapping[str, Any]) -> bool:
        # Keep family linkage/replay history. Explicit recovery/retention is separate.
        return await session.scalar(sa.select(sessions.c.id).where(sessions.c.pairing_id == row['id'])) is not None
