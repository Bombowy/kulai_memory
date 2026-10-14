"""Verified archive/restore -> atomic invalidation -> closed manual-release seal.

No migration, credential deletion, retry of COMMIT, or automatic trust restoration.
"""
import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from kulai_memory.backup_service import sha256_file, drain_before_cancellation
from kulai_memory.database_safety import (create_owned_temporary_database, drop_owned_temporary_database,
    restore_archive_to_owned_database, async_database_url, database_snapshot_url, run_database_doctor,
    validate_backup_output)
from kulai_memory.security.models import SecurityError
from .gate import RecoveryGate, canonical, checkpoint, locked

TABLES = ('kulai_security_devices', 'kulai_security_tokens', 'kulai_security_pairings',
          'kulai_security_pairing_rate', 'kulai_security_sessions', 'kulai_security_refreshes')


@dataclass(frozen=True)
class BackupEvidence:
    archive: Path
    size_bytes: int
    sha256: str
    snapshot: dict

    @classmethod
    def from_backup(cls, backup):
        return cls(backup.path, backup.size_bytes, backup.sha256, asdict(backup.source_snapshot))

    def validate(self):
        path = validate_backup_output(self.archive, force=True)
        if (type(self.size_bytes) is not int or self.size_bytes < 1 or
                not re.fullmatch('[0-9a-f]{64}', self.sha256) or path.stat().st_size != self.size_bytes or
                sha256_file(path) != self.sha256):
            raise SecurityError()
        return path

    def write_manifest(self, path):
        path=Path(path)
        if not path.is_absolute() or path.resolve()!=path or path.is_relative_to(Path(__file__).resolve().parents[4]): raise SecurityError()
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump({'archive': str(self.archive), 'size_bytes': self.size_bytes,
                       'sha256': self.sha256, 'snapshot': self.snapshot}, output)
            output.flush()
            os.fsync(output.fileno())


def canonical_part(snapshot):
    return {name: snapshot[name] for name in ('revision', 'memories', 'vectors', 'tombstones')}


async def verify_archive(evidence, *, config):
    """Actual pg_restore into a new capability-owned DB, never into the target."""
    evidence.validate()
    owned = None
    try:
        owned = await create_owned_temporary_database(kind='restore', config=config)
        await restore_archive_to_owned_database(evidence.archive, owned, config=config)
        url = async_database_url(database=owned.name, config=config)
        if not (await run_database_doctor(async_url=url, require_security=True)).ok: raise SecurityError()
        restored = asdict(await database_snapshot_url(url))
        if canonical(restored) != canonical(evidence.snapshot): raise SecurityError()
        if restored['security']['revision'] != ('kulai_security_0003',): raise SecurityError()
        evidence.validate()
        return restored
    finally:
        if owned is not None:
            await drain_before_cancellation(asyncio.create_task(drop_owned_temporary_database(owned, config=config)))


async def recover(config, gate, evidence):
    # Exclusive ingress lease is a real OS proof that participating ingress and
    # security writers are stopped. Unknown/busy lease never grants recovery.
    async with locked(gate.ingress, exclusive=True, timeout=.2), locked(gate.mutex):
        prior = gate.read()
        gate.append({**prior, 'state': 'CLOSED'})  # Durable even if verification fails.
        if not (await run_database_doctor(async_url=config.async_url, require_security=True)).ok: raise SecurityError()
        before = asdict(await database_snapshot_url(config.async_url))
        evidence.validate()
        if canonical(canonical_part(before)) != canonical(canonical_part(evidence.snapshot)): raise SecurityError()
        engine = create_async_engine(config.async_url, echo=False, hide_parameters=True,
            connect_args={'server_settings': {'statement_timeout': '10000', 'lock_timeout': '3000'}})
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            # Re-running a completed recovery verifies the same archive/hash and
            # restored state; it retains original revoked_at/consumed_at values.
            async with factory() as read:
                async with read.begin():
                    await read.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                    value = await checkpoint(await read.connection())
            if prior['state'] == 'RECOVERED' and gate.matches(prior, value) and prior.get('backup') == evidence.sha256:
                gate.append({**prior, 'state': 'RECOVERED'})
                return {'recovered': True, 'idempotent': True}
            await drain_before_cancellation(asyncio.create_task(verify_archive(evidence, config=config)))
            async with factory() as session:
                await session.begin()
                # Lock both security and canonical writers; no canonical DML/DDL.
                await session.execute(text('LOCK TABLE ' + ','.join('public.' + name for name in TABLES) + ' IN ACCESS EXCLUSIVE MODE'))
                await session.execute(text('LOCK TABLE public.memories,public.kulai_vector_records,public.memory_ingestion_tombstones IN SHARE MODE'))
                # Target must still be the exact snapshot verified above.
                if not gate.matches({'identity': value[0], 'security': value[1]}, await checkpoint(await session.connection())):
                    raise SecurityError()
                await session.execute(text("UPDATE public.kulai_security_devices SET status='revoked' WHERE status<>'revoked'"))
                await session.execute(text('UPDATE public.kulai_security_tokens SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL'))
                await session.execute(text('UPDATE public.kulai_security_sessions SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL'))
                await session.execute(text('UPDATE public.kulai_security_refreshes SET consumed_at=GREATEST(clock_timestamp(),issued_at) WHERE consumed_at IS NULL'))
                # Preserve binding/state/approval/token history and constraints.
                # A max-attempt capability is terminal in the existing proof contract.
                await session.execute(text("UPDATE public.kulai_security_pairings SET attempts=8 WHERE state IN ('invited','pending','approved') AND attempts<8"))
                after = await checkpoint(await session.connection())
                await session.commit()  # Never retry; external state stays CLOSED on ambiguity.
            latest = asdict(await database_snapshot_url(config.async_url))
            if canonical(canonical_part(latest)) != canonical(canonical_part(before)): raise SecurityError()
            gate.append({'state': 'RECOVERED', 'identity': after[0], 'security': after[1],
                         'canonical': canonical_part(before), 'backup': evidence.sha256, 'operation': str(uuid4())})
            return {'recovered': True, 'idempotent': False}
        finally:
            await engine.dispose()


async def release(config, gate):
    """Separate explicit operator action, while ingress is still stopped."""
    async with locked(gate.ingress, exclusive=True, timeout=.2), locked(gate.mutex):
        state = gate.read()
        if state['state'] != 'RECOVERED': raise SecurityError()
        if not (await run_database_doctor(async_url=config.async_url, require_security=True)).ok: raise SecurityError()
        snapshot = asdict(await database_snapshot_url(config.async_url))
        if canonical(canonical_part(snapshot)) != canonical(state['canonical']): raise SecurityError()
        engine = create_async_engine(config.async_url, echo=False, hide_parameters=True)
        try:
            async with engine.connect() as connection:
                await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                if not gate.matches(state, await checkpoint(connection)): raise SecurityError()
                unsafe = await connection.scalar(text("""SELECT
                    (SELECT count(*) FROM public.kulai_security_devices WHERE status<>'revoked') +
                    (SELECT count(*) FROM public.kulai_security_tokens WHERE revoked_at IS NULL) +
                    (SELECT count(*) FROM public.kulai_security_sessions WHERE revoked_at IS NULL) +
                    (SELECT count(*) FROM public.kulai_security_refreshes WHERE consumed_at IS NULL) +
                    (SELECT count(*) FROM public.kulai_security_pairings WHERE state IN ('invited','pending','approved') AND attempts<8)"""))
                await connection.rollback()
                if unsafe: raise SecurityError()
            gate.append({**state, 'state': 'OPEN'})
        finally: await engine.dispose()
