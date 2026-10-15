"""Actual owned PostgreSQL and unchanged gate, including near-full journal."""
import asyncio
from dataclasses import asdict

import pytest
import sqlalchemy as sa

from backend.tests.remote_tls_fixtures import certificates, prepare_owned_gate
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.security_gate_benchmark import seed
from backend.tests.journal_aging_benchmark import fill_fixture
from backend.tests.integration.test_remote_ingress_tls import application
from backend.tests.integration.test_remote_ingress_tls import websocket
from backend.tests.remote_tls_fixtures import tls_server
from backend.tests.test_memory_ws import _start_message, _stop_message
from kulai_memory.database_safety import database_snapshot_url
from kulai_memory.security import Authenticator, CredentialManager, SecurityError
from kulai_memory.security_persistence.sessions import PostgresSessionStore
from kulai_memory.security_persistence.integrity import security_snapshot, _fingerprint
from kulai_memory.security_recovery.gate import RecoveryGate


def test_near_full_journal_denies_write_before_mutation_and_corruption_denies_reads(certificates):
    async def run():
        async with owned_store() as (owned, base, target, factory, _):
            credentials = await seed(owned, base, factory, 10)
            app, _ = application(target, factory, certificates.config())
            await prepare_owned_gate(app)
            gate = RecoveryGate(certificates.directory/'gate')
            state = gate.read()
            state = {k:v for k,v in state.items() if k not in ('seq','previous','checksum')}
            before = asdict(await database_snapshot_url(target.async_url))
            fill_fixture(gate, state)
            original = gate.journal.read_bytes()
            store = PostgresSessionStore(factory, recovery_gate=gate)
            assert await Authenticator(store).authenticate(credentials[0].access_token)
            with pytest.raises(SecurityError):
                await CredentialManager(store).revoke_device(credentials[0].device_id)
            assert gate.journal.read_bytes() == original
            assert asdict(await database_snapshot_url(target.async_url)) == before
            for broken in (original[:-1], original[:100]+b'!'+original[101:]):
                gate.journal.write_bytes(broken)
                with pytest.raises(SecurityError): await Authenticator(store).authenticate(credentials[0].access_token)
            assert asdict(await database_snapshot_url(target.async_url)) == before
    asyncio.run(asyncio.wait_for(run(), 120))


def test_concurrent_gate_operations_and_rewound_checkpoint_deny_immediately(certificates):
    async def run():
        async with owned_store() as (owned, base, target, factory, _):
            credentials = await seed(owned, base, factory, 10)
            app, _ = application(target, factory, certificates.config())
            await prepare_owned_gate(app)
            gate = RecoveryGate(certificates.directory/'gate')
            old = gate.journal.read_bytes()
            store = PostgresSessionStore(factory, recovery_gate=gate)
            async def read(): assert await Authenticator(store).authenticate(credentials[0].access_token)
            async def write():
                async with store._transaction() as db: await db.execute(sa.text('SELECT 1'))
            await asyncio.gather(*(read() for _ in range(4)), *(write() for _ in range(3)))
            assert gate.read()['state'] == 'OPEN'
            await CredentialManager(store).revoke_device(credentials[0].device_id)
            with pytest.raises(SecurityError): await read()
            # Even a valid old chain is not authority after DB security changes.
            gate.journal.write_bytes(old)
            with pytest.raises(SecurityError): await read()
            assert gate.read()['state'] == 'CLOSED'
    asyncio.run(asyncio.wait_for(run(), 60))


def test_four_real_wss_clients_revoke_disconnect_and_exactly_two_saves(certificates):
    async def run():
        import json
        from uuid import uuid4
        from contextlib import AsyncExitStack
        from websockets.exceptions import ConnectionClosed
        async with owned_store() as (owned, base, target, factory, _):
            credentials = await seed(owned, base, factory, 10)
            app, runtime = application(target, factory, certificates.config())
            async with tls_server(app) as (port, _), AsyncExitStack() as stack:
                clients = [await stack.enter_async_context(websocket(certificates, port, '/ws/memory', c)) for c in credentials[:4]]
                for ws in clients:
                    await ws.send(json.dumps(_start_message(uuid4())))
                    assert json.loads(await ws.recv())['type'] == 'session.ready'
                for _ in range(5):
                    await asyncio.gather(*(ws.send(b'\0'*6400) for ws in clients))
                    await asyncio.sleep(.2)
                await CredentialManager(PostgresSessionStore(factory)).revoke_device(credentials[0].device_id)
                await clients[0].send(b'\0'*6400)
                with pytest.raises(ConnectionClosed) as denied:
                    await asyncio.wait_for(clients[0].recv(), 5)
                assert denied.value.rcvd.code == 1008
                clients[1].transport.abort()
                for ws in clients[2:]: await ws.send(json.dumps(_stop_message()))
                for ws in clients[2:]:
                    events = [json.loads(await asyncio.wait_for(ws.recv(), 10)) for _ in range(3)]
                    assert events[-1]['type'] == 'memory.saved'
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT count(*) FROM memories')) == 2
            assert len(runtime.ingest_calls) == 2
    asyncio.run(asyncio.wait_for(run(), 45))


def test_real_wss_burst_has_bounded_queue_and_disconnect_without_save(certificates):
    async def run():
        import json
        from uuid import uuid4
        from websockets.exceptions import ConnectionClosed
        async with owned_store() as (owned, base, target, factory, _):
            credentials = await seed(owned, base, factory, 10)
            app, runtime = application(target, factory, certificates.config())
            async with tls_server(app) as (port, host):
                high_water = 0
                paused = False
                def observe(protocol):
                    original_put = protocol.queue.put_nowait
                    def put(message):
                        nonlocal high_water, paused
                        original_put(message)
                        high_water = max(high_water, protocol.queue.qsize())
                        paused |= not protocol.transport.is_reading()
                    protocol.queue.put_nowait = put
                async with websocket(certificates, port, '/ws/memory', credentials[0]) as ws:
                    await ws.send(json.dumps(_start_message(uuid4())))
                    assert json.loads(await ws.recv())['type'] == 'session.ready'
                    for protocol in tuple(host.server_state.connections):
                        if hasattr(protocol, 'queue'): observe(protocol)
                    # Unpaced overload is intentionally different from Android's
                    # real cadence. Existing protocol queue cap remains eight.
                    with pytest.raises(ConnectionClosed):
                        for _ in range(128): await ws.send(b'\0'*6400)
                        await asyncio.wait_for(ws.recv(), 5)
                # Eight queued data messages plus one lifecycle disconnect.
                assert 1 <= high_water <= 9
                assert paused
                print(json.dumps(dict(burst_queue_high_water=high_water, transport_read_paused=paused,
                                      saved_memories=0)))
            assert runtime.ingest_calls == []
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT count(*) FROM memories')) == 0
    asyncio.run(asyncio.wait_for(run(), 30))


def test_partition_fingerprint_matches_historical_encoding_and_detects_edits(certificates):
    async def run():
        import hashlib
        import json
        from datetime import timezone
        from uuid import UUID
        async with owned_store() as (owned, base, _, factory, _):
            # Cross multiple 256-row partitions; 1000 is a stress fixture, not
            # a change to the real pairing admission limit of 128 records.
            await seed(owned, base, factory, 1000)
            async with factory() as db:
                await db.execute(sa.text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                connection = await db.connection()
                snapshot = await security_snapshot(connection)
                for name in ('devices','tokens','pairings','pairing_rate','sessions','refreshes'):
                    table = 'kulai_security_'+name
                    digest = hashlib.sha256(); count = 0
                    result = await connection.stream(sa.text(f'SELECT * FROM public.{table} ORDER BY id'))
                    async for row in result:
                        values = {}
                        for key, value in row._mapping.items():
                            if hasattr(value, 'utcoffset'): value = value.astimezone(timezone.utc).isoformat()
                            elif value is not None and (isinstance(value, UUID) or key in {'id','device_id','project_id','client_id','token_id'}): value = str(value)
                            values[key] = value
                        payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()
                        digest.update(len(payload).to_bytes(8, 'big')); digest.update(payload); count += 1
                    assert asdict(getattr(snapshot, name)) == dict(count=count, sha256=digest.hexdigest())
                await db.rollback()
            async with factory.begin() as db:
                await db.execute(sa.text("UPDATE kulai_security_devices SET status='revoked' WHERE id=(SELECT id FROM kulai_security_devices LIMIT 1)"))
            async with factory() as db:
                assert await _fingerprint(await db.connection(), 'kulai_security_devices') != snapshot.devices
    asyncio.run(asyncio.wait_for(run(), 60))


def test_aged_gate_replacement_requires_verified_restore_recovery_and_new_pairing(certificates):
    async def run():
        from kulai_memory.backup_service import create_verified_backup
        from kulai_memory.security_recovery.recovery import BackupEvidence, recover, release, canonical_part
        from kulai_memory.security_recovery.gate import locked
        from backend.tests.integration.test_sessions_postgres import paired
        from kulai_memory.security import Action
        async with owned_store() as (owned, base, target, factory, _):
            old = (await seed(owned, base, factory, 10))[0]
            app, _ = application(target, factory, certificates.config())
            await prepare_owned_gate(app)
            gate = RecoveryGate(certificates.directory/'gate')
            state = {k:v for k,v in gate.read().items() if k not in ('seq','previous','checksum')}
            fill_fixture(gate, state, records=1000)
            before = asdict(await database_snapshot_url(target.async_url))
            # Ingress is never started in this test. Exclusive lease proves
            # participating workers are stopped before closing the old journal.
            async with locked(gate.ingress), locked(gate.mutex):
                gate.append({**gate.read(), 'state':'CLOSED'})
            archive = await create_verified_backup(certificates.directory/'rollover.dump',config=base,owned=owned,app_env='test')
            new = RecoveryGate.initialize(certificates.directory/'next-gate')
            sealed = PostgresSessionStore(factory,recovery_gate=new)
            with pytest.raises(SecurityError): await Authenticator(sealed).authenticate(old.access_token)
            with pytest.raises(SecurityError): await release(target,new)
            await recover(target,new,BackupEvidence.from_backup(archive))  # real owned restore verification
            assert new.read()['state'] == 'RECOVERED'
            await release(target,new)  # explicit test action, no launcher/autostart
            with pytest.raises(SecurityError): await Authenticator(sealed).authenticate(old.access_token)
            factory.configure(info={'recovery_gate':new})
            issued = (await paired(factory,actions=frozenset(Action)))[-1]
            assert await Authenticator(sealed).authenticate(issued.access_token)
            after = asdict(await database_snapshot_url(target.async_url))
            assert canonical_part(before) == canonical_part(after)
            assert gate.read()['state'] == 'CLOSED'  # Retain complete old audit chain.
    asyncio.run(asyncio.wait_for(run(), 120))
