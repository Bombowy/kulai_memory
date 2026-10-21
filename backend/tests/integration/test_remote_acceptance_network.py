"""Real TLS/WSS network/commit boundaries on capability-owned PostgreSQL."""
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json
from uuid import uuid4
import wave

import pytest
import sqlalchemy as sa
from websockets.exceptions import ConnectionClosed

from backend.tests.remote_tls_fixtures import certificates,tls_server,prepare_owned_gate
from backend.tests.integration.test_remote_ingress_tls import pipeline,application,websocket,client,headers
from backend.tests.integration.test_sessions_postgres import paired
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.security_gate_benchmark import seed
from backend.tests.test_memory_ws import _start_message,_stop_message
from kulai_memory.security import Action,CredentialManager
from kulai_memory.security.authentication import utc_now
from kulai_memory.security_persistence.sessions import PostgresSessionStore
from kulai_memory.security_recovery.gate import RecoveryGate,locked


async def note(ws,ingestion):
    await ws.send(json.dumps(_start_message(ingestion)))
    assert json.loads(await ws.recv())['type']=='session.ready'


async def saved(ws):
    await ws.send(json.dumps(_stop_message()))
    events=[json.loads(await asyncio.wait_for(ws.recv(),10)) for _ in range(3)]
    assert events[-1]['type']=='memory.saved'


@pytest.mark.parametrize('delivery',['tcp_pause','coalesced','fragmented'])
def test_actual_tls_pause_batch_and_fragmented_messages_preserve_pcm(certificates,delivery):
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            credential=(await paired(factory,actions=frozenset(Action)))[-1]
            app,runtime=application(target,factory,certificates.config())
            original=runtime.transcribe
            verified=[]; paths=[]
            # Valid PCM16 bytes with non-uniform content: reversed fragments or
            # a misplaced boundary cannot silently pass as identical silence.
            data=bytes(range(256))*25
            async def transcribe(**kwargs):
                paths.append(kwargs['audio_path'])
                with wave.open(str(kwargs['audio_path']),'rb') as source: actual=source.readframes(source.getnframes())
                assert actual==data*3; verified.append(True)
                return await original(**kwargs)
            runtime.transcribe=transcribe
            async with tls_server(app) as (port,host),websocket(certificates,port,'/ws/memory',credential) as ws:
                await note(ws,uuid4())
                if delivery=='tcp_pause':
                    # Pause the real TLS transport's TCP reads, not a mocked
                    # identity. Resume the same connection after a short gap.
                    peer=next(p for p in host.server_state.connections if hasattr(p,'queue'))
                    peer.transport.pause_reading()
                    try:
                        for _ in range(3): await ws.send(data)
                        await asyncio.sleep(.6)
                    finally: peer.transport.resume_reading()
                elif delivery=='coalesced':
                    for _ in range(3): await ws.send(data)
                else:
                    for _ in range(3): await ws.send([data[:1000],data[1000:]])
                await saved(ws)
            assert verified==[True] and all(not path.exists() for path in paths)
            async with factory() as db: assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==1
    asyncio.run(asyncio.wait_for(run(),30))


@pytest.mark.parametrize('after_commit',[False,True])
def test_disconnect_during_save_no_automatic_retry_manual_same_ingestion_is_idempotent(certificates,after_commit):
    async def run():
        async with pipeline(certificates) as (port,_,runtime,factory,_,credential):
            entered,drained=asyncio.Event(),asyncio.Event()
            original=runtime.ingest; calls=[]; ingestion=uuid4()
            async def delayed(**kwargs):
                calls.append(True)
                if after_commit: await original(**kwargs)
                entered.set()
                try: await asyncio.Event().wait()
                finally: drained.set()
            runtime.ingest=delayed
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await note(ws,ingestion); await ws.send(b'\0'*6400)
                await ws.send(json.dumps(_stop_message()))
                await asyncio.wait_for(entered.wait(),5); ws.transport.abort()
            await asyncio.wait_for(drained.wait(),5)
            assert len(calls)==1
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==int(after_commit)
            runtime.ingest=original
            # Explicit operator/client retry, never library auto-retry. Same
            # ingestion ID and bytes retain the existing canonical API contract.
            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                await note(ws,ingestion); await ws.send(b'\0'*6400); await saved(ws)
            async with factory() as db:
                assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==1
                assert str(await db.scalar(sa.text('SELECT ingestion_id FROM memories')))==str(ingestion)
    asyncio.run(asyncio.wait_for(run(),35))


def test_wall_clock_access_expiry_during_real_wss_denies_next_frame(certificates):
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            credential=(await paired(factory,actions=frozenset(Action),ttl=timedelta(seconds=10)))[-1]
            app,runtime=application(target,factory,certificates.config())
            async with tls_server(app) as (port,_):
                async with websocket(certificates,port,'/ws/memory',credential) as ws:
                    await note(ws,uuid4())
                    with pytest.raises(ConnectionClosed) as denial:
                        while utc_now()<credential.expires_at+timedelta(seconds=1):
                            await ws.send(b'\0'*6400); await asyncio.sleep(.2)
                        await asyncio.wait_for(ws.recv(),3)
                    assert denial.value.rcvd.code==1008
                assert runtime.ingest_calls==[]
    asyncio.run(asyncio.wait_for(run(),30))


def test_rewound_valid_journal_closes_multiple_live_tls_connections(certificates):
    async def run():
        async with owned_store() as (owned,base,target,factory,_):
            credentials=await seed(owned,base,factory,10)
            app,runtime=application(target,factory,certificates.config())
            await prepare_owned_gate(app); gate=RecoveryGate(certificates.directory/'gate')
            old=gate.journal.read_bytes()
            async with tls_server(app) as (port,_),AsyncExitStack() as stack:
                streams=[await stack.enter_async_context(websocket(certificates,port,'/ws/memory',c)) for c in credentials[:3]]
                for ws in streams: await note(ws,uuid4())
                await CredentialManager(PostgresSessionStore(factory)).revoke_device(credentials[0].device_id)
                async with locked(gate.mutex): gate.journal.write_bytes(old)  # Isolated fixture rollback attack.
                for ws in streams:
                    try: await ws.send(b'\0'*6400)
                    except ConnectionClosed: pass
                for ws in streams:
                    with pytest.raises(ConnectionClosed): await asyncio.wait_for(ws.recv(),5)
                assert gate.read()['state']=='CLOSED' and runtime.ingest_calls==[]
    asyncio.run(asyncio.wait_for(run(),35))


def test_restart_ingress_revalidates_same_gate_and_authenticates_new_connection(certificates):
    async def run():
        async with owned_store() as (_,_,target,factory,_):
            credential=(await paired(factory,actions=frozenset(Action)))[-1]
            for _ in range(2):
                app,_=application(target,factory,certificates.config())
                async with tls_server(app) as (port,_):
                    async with client(certificates,port) as transport:
                        assert (await transport.get('/api/memories',headers=headers(credential))).status_code==200
                        assert (await transport.get('/api/memories')).status_code==401
    asyncio.run(asyncio.wait_for(run(),35))


def test_real_harness_cancellation_drops_owned_database_and_removes_audio(monkeypatch):
    async def run():
        from backend.tests import security_postgres_fixtures as fixtures
        from backend.tests import security_stream_benchmark as benchmark
        from kulai_memory.api import memory_ws
        from kulai_memory.database_safety import database_config, database_exists
        created=asyncio.Event(); paths=[]; databases=[]
        real_wav=memory_ws.OwnedPcmWav
        def observe_wav():
            value=real_wav(); paths.append(value.path); created.set(); return value
        real_create=fixtures.create_owned_temporary_database
        async def observe_database(**kwargs):
            value=await real_create(**kwargs); databases.append(value); return value
        monkeypatch.setattr(memory_ws,'OwnedPcmWav',observe_wav)
        monkeypatch.setattr(fixtures,'create_owned_temporary_database',observe_database)
        task=asyncio.create_task(benchmark.matrix(10,60,[1]))
        try:
            await asyncio.wait_for(created.wait(),15)
            await asyncio.sleep(.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        assert paths and all(not path.exists() for path in paths)
        assert len(databases)==1
        assert not await database_exists(databases[0].name, config=database_config())
    asyncio.run(asyncio.wait_for(run(),35))
