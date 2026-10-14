"""Opt-in real PostgreSQL/recovery-gate benchmark, never writes the configured DB.

Run with the backend SDK: -m backend.tests.security_gate_benchmark --output ABS.json.
Only marker-owned temporary DBs and temporary PKI/gates receive writes. Production
authentication code is unchanged. Results contain aggregates, never credentials.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import secrets
import statistics
import subprocess
import tempfile
import time
import traceback
from uuid import uuid4

import sqlalchemy as sa

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_sessions_postgres import paired
from backend.tests.integration.test_remote_ingress_tls import application, websocket
from backend.tests.remote_tls_fixtures import Certificates, prepare_owned_gate, tls_server
from backend.tests.test_memory_ws import _start_message, _stop_message
from kulai_memory.database_safety import database_config, database_snapshot_url, require_owned_database
from kulai_memory.pairing.domain import PairingService, MAX_SESSIONS
from kulai_memory.security import Action, Authenticator, CredentialManager, SecurityError
from kulai_memory.security.authentication import new_credential, utc_now
from kulai_memory.security.sessions import IssuedSessionCredential, SessionService, new_refresh
from kulai_memory.security_persistence.integrity import security_snapshot
from kulai_memory.security_persistence.postgres import _values
from kulai_memory.security_persistence.schema_v1 import devices, tokens
from kulai_memory.security_persistence.schema_v2 import pairings
from kulai_memory.security_persistence.schema_v3 import sessions, refreshes
from kulai_memory.security_persistence.sessions import PostgresSessionStore
from kulai_memory.security_recovery.gate import MAX_JOURNAL, RecoveryGate, checkpoint


def distribution(values):
    values=sorted(values)
    return dict(n=len(values),min_ms=round(values[0],3),median_ms=round(statistics.median(values),3),
                p95_ms=round(values[math.ceil(len(values)*.95)-1],3),max_ms=round(values[-1],3))


def memory():
    if os.name!='nt': return {'result':'NOT VERIFIED','reason':'Windows memory sampler unavailable'}
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
            (name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
            'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]
    get_process=ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype=wintypes.HANDLE
    function=ctypes.windll.psapi.GetProcessMemoryInfo
    function.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    function.restype=wintypes.BOOL
    value=Counters(); value.cb=ctypes.sizeof(value)
    if not function(get_process(),ctypes.byref(value),value.cb): raise RuntimeError('Memory sampling unavailable')
    return dict(working_set_bytes=value.WorkingSetSize,peak_working_set_bytes=value.PeakWorkingSetSize,private_bytes=value.PrivateUsage)


def postgres_resources():
    if os.name!='nt': return None
    command="@(Get-Process -Name postgres -ErrorAction Stop | Select-Object Id,CPU,WorkingSet64,PrivateMemorySize64) | ConvertTo-Json -Compress"
    value=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],capture_output=True,timeout=15)
    if value.returncode: return None
    rows=json.loads(value.stdout)
    return rows if isinstance(rows,list) else [rows]


def resource_delta(before,after):
    if not before or not after: return {'result':'NOT VERIFIED'}
    first={r['Id']:r for r in before}; last={r['Id']:r for r in after}
    common=first.keys() & last.keys()
    readable=[k for k in common if isinstance(first[k]['CPU'],(int,float)) and isinstance(last[k]['CPU'],(int,float))]
    return dict(same_process_cpu_seconds=round(sum(last[k]['CPU']-first[k]['CPU'] for k in readable),4) if readable else None,
                cpu_readable_processes=len(readable),
                common_processes=len(common),started_processes=len(last.keys()-first.keys()),
                exited_processes=len(first.keys()-last.keys()),
                rss_before_bytes=sum(r['WorkingSet64'] for r in before),rss_after_bytes=sum(r['WorkingSet64'] for r in after),
                scope='All local PostgreSQL processes; CPU delta excludes exited/new PIDs and includes other DB activity')


async def seed(owned,base,factory,size):
    """Bulk synthetic fixture enrollment before gate. All constraints remain active.

    One real approved pairing supplies a valid row prototype. Extra rows have
    distinct standard credentials, valid foreign keys, and realistic past issuance.
    This avoids weakening pairing abuse/rate limits to manufacture a large fixture.
    """
    await require_owned_database(owned,config=base)
    _,_,_,_,first=await paired(factory,actions=frozenset(Action))
    tables=(devices,tokens,pairings,sessions,refreshes)
    async with factory() as db:
        assert await db.scalar(sa.text('SELECT current_database()'))==owned.name
        prototypes=[dict((await db.execute(sa.select(table))).mappings().one()) for table in tables]
        await db.rollback()
    rows=[[] for _ in tables]; credentials=[first]
    for _ in range(size-1):
        d,t,p,s,r=[{k:v-timedelta(seconds=60) if isinstance(v,datetime) else v for k,v in original.items()} for original in prototypes]
        did,pid,sid=uuid4(),uuid4(),uuid4()
        record,issued=new_credential(did,timedelta(hours=1),frozenset(Action),None,t['issued_at'])
        rid,refresh_secret,refresh_hash=new_refresh()
        d['id']=did
        t.update(_values(record))
        p.update(id=pid,device_id=did,client_id=uuid4(),token_id=record.id,
                 invite_digest=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                 receiver_digest=hashlib.sha256(secrets.token_bytes(32)).hexdigest())
        s.update(id=sid,device_id=did,pairing_id=pid)
        r.update(id=rid,session_id=sid,access_id=record.id,digest=refresh_hash)
        for target,row in zip(rows,(d,t,p,s,r)): target.append(row)
        credentials.append(IssuedSessionCredential(did,record.id,issued.access_token,record.expires_at,
            sid,rid,refresh_secret,r['expires_at'],s['absolute_expires_at']))
    async with factory.begin() as db:
        assert await db.scalar(sa.text('SELECT current_database()'))==owned.name
        for table,batch in zip(tables,rows):
            if batch: await db.execute(sa.insert(table),batch)
    return credentials


async def paced_pcm(app,runtime,certificates,credential,factory,seconds):
    timings=[]; jitter=[]; send_times=[]; frames=seconds*5; frame=b'\0'*6400
    async with tls_server(app) as (port,_):
        security=app.state.security_runtime
        original=security.lookup
        async def observed(digest):
            start=time.perf_counter()
            try: return await original(digest)
            finally: timings.append((time.perf_counter()-start)*1000)
        security.lookup=observed  # instrumentation only; delegates every decision
        pg_before=postgres_resources(); cpu=time.process_time(); before_memory=memory()
        async with websocket(certificates,port,'/ws/memory',credential) as ws:
            await ws.send(json.dumps(_start_message(uuid4())))
            assert json.loads(await ws.recv())['type']=='session.ready'
            started=time.perf_counter()
            for number in range(1,frames+1):
                deadline=started+number*.2
                await asyncio.sleep(max(0,deadline-time.perf_counter()))
                jitter.append(max(0,time.perf_counter()-deadline)*1000)
                send_start=time.perf_counter()
                await ws.send(frame)
                send_times.append((time.perf_counter()-send_start)*1000)
                if number%300==0:
                    print(json.dumps({'stage':'paced_pcm','sent_seconds':number//5,'frames':number}),flush=True)
            sent_seconds=time.perf_counter()-started
            stopped=time.perf_counter()
            await ws.send(json.dumps(_stop_message()))
            events=[json.loads(await asyncio.wait_for(ws.recv(),180)) for _ in range(3)]
            assert events[-1]['type']=='memory.saved'
            stop_latency=time.perf_counter()-stopped
        assert len(timings)>=frames
        assert len(runtime.ingest_calls)==1
        async with factory() as db:
            assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==1
        cpu_seconds=time.process_time()-cpu
        elapsed=time.perf_counter()-started
        return dict(result='PASS',paced_seconds=round(sent_seconds,3),frames=frames,pcm_bytes=frames*len(frame),
            frame_interval_ms=200,auth_lookups=len(timings),auth=distribution(timings),send=distribution(send_times),
            scheduling_lateness=distribution(jitter),stop_to_saved_seconds=round(stop_latency,3),
            cpu_seconds=round(cpu_seconds,3),cpu_percent_one_core=round(cpu_seconds/elapsed*100,2),
            memory_before=before_memory,memory_after=memory(),postgres=resource_delta(pg_before,postgres_resources()),
            scope='real loopback mTLS WSS, owned PostgreSQL, synthetic inference; no VPS/WireGuard')


async def measure_size(size,samples,pcm_seconds):
    with tempfile.TemporaryDirectory(prefix='kulai-owned-gate-benchmark-') as directory:
        certificates=Certificates(directory)
        async with owned_store() as (owned,base,target,factory,_):
            credentials=await seed(owned,base,factory,size)
            app,runtime=application(target,factory,certificates.config())
            await prepare_owned_gate(app)
            gate=RecoveryGate(certificates.directory/'gate')
            store=PostgresSessionStore(factory,recovery_gate=gate)
            auth=Authenticator(store)
            sql=[0]
            def count(*_): sql[0]+=1
            sa.event.listen(factory.kw['bind'].sync_engine,'before_cursor_execute',count)
            output={'seeded_sessions':size,'samples_requested':samples,'memory_before':memory(),'metrics':{},
                    'pairing_record_policy_limit':MAX_SESSIONS,'dataset_above_pairing_policy_limit':size>=MAX_SESSIONS}
            pg_before=postgres_resources(); baseline_cpu=time.process_time()
            async def measured(name,operation,n=samples):
                values=[]; cpu=time.process_time(); statement_start=sql[0]; journal_start=gate.journal.stat().st_size
                for index in range(n):
                    started=time.perf_counter(); await operation(index)
                    values.append((time.perf_counter()-started)*1000)
                output['metrics'][name]={**distribution(values),'client_cpu_seconds':round(time.process_time()-cpu,4),
                    'sql_statements':sql[0]-statement_start,'journal_growth_bytes':gate.journal.stat().st_size-journal_start}
            # Warm-up is excluded from timing; no authorization/fingerprint caching.
            await auth.authenticate(credentials[0].access_token)
            await measured('access_authentication',lambda _:auth.authenticate(credentials[0].access_token))
            await measured('checkpoint_verify',lambda _:gate.check(factory))
            async def fingerprint(_):
                async with factory() as db:
                    await db.execute(sa.text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                    await security_snapshot(await db.connection())
                    await db.rollback()
            await measured('full_security_fingerprint',fingerprint)
            async def refresh(index):
                old=credentials[index+1]
                renewed=await SessionService(store).renew(old.session_id,old.device_id,old.refresh_id,old.refresh_credential)
                assert renewed.credential is not None and renewed.credential.refresh_id!=old.refresh_id
                with_denial=False
                try: await auth.authenticate(old.access_token)
                except SecurityError: with_denial=True
                assert with_denial
                credentials[index+1]=renewed.credential
            await measured('refresh_rotation_and_predecessor_denial',refresh,n=min(4,size-2))
            pairing=PairingService(store,clock=utc_now)
            async def pair(_):
                invitation=await pairing.invite(actions=frozenset(Action))
                receiver=secrets.token_urlsafe(32)
                view=await pairing.request(invitation.session_id,invitation.secret,receiver,uuid4(),'Synthetic benchmark')
                await store.decide(view.session_id,view.device_id,approve=True)
                issued=await pairing.claim(view.session_id,receiver)
                assert await auth.authenticate(issued.access_token)
            if size>=MAX_SESSIONS:
                async def denied_pair(_):
                    denied=False
                    try: await pairing.invite(actions=frozenset(Action))
                    except SecurityError: denied=True
                    assert denied  # Preserve the live admission bound, never raise it for the benchmark.
                await measured('pairing_capacity_denial',denied_pair,n=3)
            else:
                await measured('approved_pairing_full_workflow',pair,n=3)
            async def revoke(index):
                old=credentials[-index-1]
                await CredentialManager(store).revoke_device(old.device_id)
                denied=False
                try: await auth.authenticate(old.access_token)
                except SecurityError: denied=True
                assert denied
            await measured('revocation_and_immediate_denial',revoke,n=3)
            async def write(_):
                async with store._transaction() as db: await db.execute(sa.text('SELECT 1'))
            await measured('checkpoint_write_fsync_commit',write,n=samples)
            growth=output['metrics']['checkpoint_write_fsync_commit']['journal_growth_bytes']/samples
            output['journal']={'bytes':gate.journal.stat().st_size,'limit_bytes':MAX_JOURNAL,'mean_bytes_per_write_transaction':growth,
                'estimated_write_transactions_from_empty':int(MAX_JOURNAL/growth),
                'estimated_remaining_write_transactions':int((MAX_JOURNAL-gate.journal.stat().st_size)/growth),
                'capacity_method':'Measured record growth extrapolation; not an executed 16 MiB fill test'}
            output.update(client_cpu_seconds=round(time.process_time()-baseline_cpu,4),memory_after=memory(),
                          postgres=resource_delta(pg_before,postgres_resources()))
            if pcm_seconds: output['pcm']=await paced_pcm(app,runtime,certificates,credentials[0],factory,pcm_seconds)
            await gate.check(factory)
            output['gate_final_state']=gate.read()['state']
            async with factory() as db:
                output['final_sessions']=await db.scalar(sa.select(sa.func.count()).select_from(sessions))
            return output


async def run(args):
    if os.environ.get('KULAI_RUN_POSTGRES_INTEGRATION')!='1': raise ValueError('Explicit integration opt-in required')
    if not args.output.is_absolute() or args.output.exists(): raise ValueError('New absolute output required')
    before=asdict(await database_snapshot_url(database_config().async_url))
    output={'started_at':datetime.now(timezone.utc).isoformat(),'method':'Standard security path; no auth cache or weakened runtime limits',
            'main_before':before,'sizes':[]}
    for size in args.sizes:
        print(json.dumps({'stage':'size_started','sessions':size}),flush=True)
        value=await measure_size(size,args.samples,args.pcm_seconds if size==max(args.sizes) else 0)
        output['sizes'].append(value)
        print(json.dumps({'stage':'size_completed','sessions':size,'auth':value['metrics']['access_authentication']}),flush=True)
    after=asdict(await database_snapshot_url(database_config().async_url))
    assert after==before
    output.update(main_after=after,main_unchanged=True,finished_at=datetime.now(timezone.utc).isoformat(),result='PASS')
    with args.output.open('x',encoding='utf-8') as file: json.dump(output,file,indent=2)
    print(json.dumps({'result':'PASS','output':str(args.output),'main_unchanged':True}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sizes',type=int,nargs='+',default=[10,100,1000])
    parser.add_argument('--samples',type=int,default=20)
    parser.add_argument('--pcm-seconds',type=int,choices=[0,600],default=600)
    args=parser.parse_args()
    if not 2<=args.samples<=100 or not args.sizes or any(n not in (10,100,1000) for n in args.sizes): parser.error('Bounded benchmark parameters required')
    logging.disable(logging.INFO)
    try: asyncio.run(asyncio.wait_for(run(args),2400))
    except Exception as error:
        frames=[dict(file=Path(frame.filename).name,line=frame.lineno,function=frame.name)
                for frame in traceback.extract_tb(error.__traceback__)]
        print(json.dumps({'result':'FAIL','error_type':type(error).__name__,'frames':frames,
                          'detail':'Benchmark failed; owned cleanup requested; exception values/credentials omitted.'}),flush=True)
        return 1
    return 0

if __name__=='__main__': raise SystemExit(main())
