"""Opt-in paced concurrent real mTLS/WSS + owned PostgreSQL measurement.

Observation delegates every authorization, including every PCM frame. Never
changes production timeouts, revocation, checkpoint policy or audio format.
"""
import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

import sqlalchemy as sa

from backend.tests.security_gate_benchmark import seed, memory, distribution
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.remote_tls_fixtures import Certificates, tls_server, prepare_owned_gate
from backend.tests.integration.test_remote_ingress_tls import application, websocket
from backend.tests.test_memory_ws import _start_message, _stop_message
from kulai_memory.database_safety import database_config, database_snapshot_url, run_database_doctor
from kulai_memory.security.authentication import token_digest


def percentiles(values):
    import math
    return {**distribution(values), 'p99_ms': round(sorted(values)[math.ceil(.99*len(values))-1], 3)}


async def matrix(size, seconds, counts):
    with tempfile.TemporaryDirectory(prefix='kulai-owned-stream-') as directory:
        certificates = Certificates(directory)
        async with owned_store() as (owned, base, target, factory, _):
            credentials = await seed(owned, base, factory, size)
            app, runtime = application(target, factory, certificates.config())
            await prepare_owned_gate(app)
            results = []
            async with tls_server(app) as (port, _):
                security = app.state.security_runtime
                original = security.lookup
                sql_context = ContextVar('stream_sql_count', default=None)
                def count_sql(*_):
                    counter = sql_context.get()
                    if counter is not None: counter[0] += 1
                sa.event.listen(security._engine.sync_engine, 'before_cursor_execute', count_sql)
                tracks = {}
                async def observed(digest):
                    track = tracks.get(digest)
                    counter = [0]
                    token = sql_context.set(counter)
                    began = time.perf_counter()
                    frame = None
                    if track is not None and track['seen'] < len(track['sent']):
                        frame = track['sent'][track['seen']]
                        track['seen'] += 1
                    try:
                        return await original(digest)
                    finally:
                        ended = time.perf_counter()
                        sql_context.reset(token)
                        if frame is not None:
                            track['auth'].append((ended-began)*1000)
                            track['latency'].append((ended-frame)*1000)
                            track['sql'].append(counter[0])
                            track['done'] += 1
                security.lookup = observed
                for clients in counts:
                    tracks.clear()
                    ready = 0
                    begin = asyncio.Event()
                    frames = seconds*5
                    cpu = time.process_time()
                    rss = memory()
                    async with factory() as db:
                        previous = await db.scalar(sa.text('SELECT count(*) FROM memories'))
                    async def client(index):
                        nonlocal ready
                        credential = credentials[index]
                        track = dict(sent=[], auth=[], latency=[], sql=[], seen=0, done=0,
                                     backlog=[], send=[], late=[], stop=None)
                        async with websocket(certificates, port, '/ws/memory', credential) as ws:
                            await ws.send(json.dumps(_start_message(uuid4())))
                            assert json.loads(await ws.recv())['type'] == 'session.ready'
                            tracks[token_digest(credential.access_token)] = track
                            ready += 1
                            await begin.wait()
                            start = time.perf_counter()
                            for number in range(1, frames+1):
                                deadline = start+number*.2
                                await asyncio.sleep(max(0, deadline-time.perf_counter()))
                                now = time.perf_counter()
                                track['late'].append(max(0, now-deadline)*1000)
                                track['sent'].append(now)
                                track['backlog'].append(len(track['sent'])-track['done'])
                                await ws.send(b'\0'*6400)
                                track['send'].append((time.perf_counter()-now)*1000)
                            stopped = time.perf_counter()
                            await ws.send(json.dumps(_stop_message()))
                            async with asyncio.timeout(90):
                                events = [json.loads(await ws.recv()) for _ in range(3)]
                            assert events[-1]['type'] == 'memory.saved'
                            track['stop'] = round(time.perf_counter()-stopped, 4)
                        assert track['done'] == frames
                        return track
                    tasks = [asyncio.create_task(client(i)) for i in range(clients)]
                    try:
                        async with asyncio.timeout(30):
                            while ready != clients:
                                for task in tasks:
                                    if task.done(): task.result()
                                await asyncio.sleep(.01)
                        started = time.perf_counter()
                        begin.set()
                        rows = await asyncio.gather(*tasks)
                    finally:
                        for task in tasks: task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    elapsed = time.perf_counter()-started
                    async with factory() as db:
                        saved = await db.scalar(sa.text('SELECT count(*) FROM memories'))
                    assert saved-previous == clients
                    output = dict(sessions=size, clients=clients, pcm_seconds=seconds,
                        frames_per_client=frames, bytes_per_frame=6400, interval_ms=200,
                        elapsed_seconds=round(elapsed, 3), saved_memories=saved-previous,
                        auth=percentiles([v for r in rows for v in r['auth']]),
                        frame_send_to_auth_end=percentiles([v for r in rows for v in r['latency']]),
                        client_send=percentiles([v for r in rows for v in r['send']]),
                        scheduling_lateness=percentiles([v for r in rows for v in r['late']]),
                        sql_per_frame_min=min(v for r in rows for v in r['sql']),
                        sql_per_frame_max=max(v for r in rows for v in r['sql']),
                        peak_unfinished_frames_per_client=[max(r['backlog']) for r in rows],
                        stop_to_saved_seconds=[r['stop'] for r in rows],
                        python_cpu_seconds=round(time.process_time()-cpu, 3),
                        memory_before=rss, memory_after=memory(),
                        backpressure='Real unchanged bounded transport; measured send await and unfinished frame queue',
                        result='PASS')
                    results.append(output)
                    print(json.dumps(output), flush=True)
                security.lookup = original
            return results


async def run(args):
    if os.environ.get('KULAI_RUN_POSTGRES_INTEGRATION') != '1': raise ValueError('Opt-in required')
    if not args.output.is_absolute() or args.output.exists(): raise ValueError('New absolute output required')
    before = asdict(await database_snapshot_url(database_config().async_url))
    report = dict(started_at=datetime.now(timezone.utc).isoformat(), label=args.label,
                  scope='Real loopback mTLS WSS, owned PG, synthetic AI; no WireGuard/VPS',
                  main_before=before, cases=[])
    for size in args.sizes:
        report['cases'].extend(await matrix(size, args.seconds, args.clients))
    after = asdict(await database_snapshot_url(database_config().async_url))
    assert before == after
    doctor = await run_database_doctor(async_url=database_config().async_url)
    assert doctor.ok
    report.update(main_after=after, main_unchanged=True, canonical_doctor_ok=True,
                  finished_at=datetime.now(timezone.utc).isoformat(), result='PASS')
    with args.output.open('x', encoding='utf-8') as stream: json.dump(report, stream, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', choices=['before', 'after', 'verification'], required=True)
    parser.add_argument('--seconds', type=int, choices=[4, 20, 60], default=20)
    parser.add_argument('--sizes', type=int, nargs='+', choices=[10, 100, 1000], default=[10, 100])
    parser.add_argument('--clients', type=int, nargs='+', choices=[1, 2, 4], default=[1, 2, 4])
    args = parser.parse_args()
    logging.disable(logging.INFO)
    try: asyncio.run(asyncio.wait_for(run(args), 1800))
    except Exception as error:
        # Exception values/locals may contain credentials: print type/locations only.
        import traceback
        print(json.dumps(dict(result='FAIL', error_type=type(error).__name__,
            frames=[dict(file=Path(f.filename).name, line=f.lineno) for f in traceback.extract_tb(error.__traceback__)])), flush=True)
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
