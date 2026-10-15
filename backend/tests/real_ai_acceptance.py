"""Explicit real installed AI acceptance over mTLS; synthetic owned data only."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
import wave

import sqlalchemy as sa

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.remote_tls_fixtures import Certificates, tls_server
from backend.tests.integration.test_remote_ingress_tls import application, client, websocket, headers
from backend.tests.integration.test_sessions_postgres import paired
from backend.tests.integration.test_desktop_voice_question_whisper import synthetic_speech
from backend.tests.test_memory_ws import _start_message, _stop_message
from backend.tests.test_server_transport import start, stop
from backend.tests.security_stream_benchmark import percentiles
from kulai_memory.database_safety import database_config, database_snapshot_url
from kulai_memory.security import Action
from kulai_memory.server.runtime import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings


def gpu():
    value = subprocess.run(['nvidia-smi.exe','--query-gpu=memory.used,utilization.gpu',
        '--format=csv,noheader,nounits'], capture_output=True, timeout=10)
    if value.returncode: return None
    values = value.stdout.decode().strip().splitlines()[0].split(',')
    return dict(vram_mib=int(values[0]), utilization_percent=int(values[1]))


async def run(output):
    if any(os.environ.get(k) != '1' for k in ('KULAI_RUN_POSTGRES_INTEGRATION','KULAI_RUN_REAL_AI_ACCEPTANCE','HF_HUB_OFFLINE')):
        raise ValueError('Explicit real-AI/owned/offline opt-in required')
    if not output.is_absolute() or output.exists(): raise ValueError('New absolute output required')
    report = dict(started_at=datetime.now(timezone.utc).isoformat(), result='FAIL', steps=[], gpu_samples=[])
    report['main_before'] = asdict(await database_snapshot_url(database_config().async_url))
    async def measured(name, operation):
        began = time.perf_counter()
        value = await operation()
        report['steps'].append(dict(name=name, seconds=round(time.perf_counter()-began,3), result='PASS'))
        print(json.dumps(report['steps'][-1]), flush=True)
        return value
    monitor = None; probes=[]; heartbeat_times=[]; tick_delays=[]
    try:
        async def sample():
            while True:
                value = await asyncio.to_thread(gpu)
                if value: report['gpu_samples'].append(value)
                await asyncio.sleep(1)
        monitor = asyncio.create_task(sample())
        with tempfile.TemporaryDirectory(prefix='kulai-owned-real-ai-') as directory:
            root = Path(directory); certificates = Certificates(root)
            raw_note = synthetic_speech(root,'The green dragon currently lives on Venus.','note.wav')
            raw_question = synthetic_speech(root,'Where does the green dragon currently live?','question.wav')
            def pcm(raw):
                with wave.open(io.BytesIO(raw),'rb') as source:
                    assert (source.getframerate(),source.getnchannels(),source.getsampwidth()) == (16000,1,2)
                    return source.readframes(source.getnframes())
            async with owned_store() as (_,_,target,factory,_):
                credential = (await paired(factory,actions=frozenset(Action)))[-1]
                settings = Settings().model_copy(update={'kulai_vector_dimension':1024})
                runtime = VoiceMemoryServerRuntime(settings=settings,database_config_factory=lambda:target)
                paths = []
                for method in ('transcribe','transcribe_question'):
                    original = getattr(runtime, method)
                    async def observed(_original=original, _method=method, **kwargs):
                        paths.append(kwargs['audio_path'])
                        return await measured(_method,lambda:_original(**kwargs))
                    setattr(runtime,method,observed)
                app,_ = application(target,factory,certificates.config(),runtime=runtime)
                began = time.perf_counter()
                async with tls_server(app) as (port,_):
                    report['runtime_start_seconds'] = round(time.perf_counter()-began,3)
                    async def heartbeat():
                        async with client(certificates,port) as connection:
                            while True:
                                began=time.perf_counter()
                                response=await connection.get('/api/memories',headers=headers(credential),timeout=10)
                                assert response.status_code==200
                                heartbeat_times.append((time.perf_counter()-began)*1000)
                                await asyncio.sleep(.5)
                    async def tick():
                        while True:
                            began=time.perf_counter(); await asyncio.sleep(.1)
                            tick_delays.append(max(0,(time.perf_counter()-began-.1)*1000))
                    probes=[asyncio.create_task(heartbeat()),asyncio.create_task(tick())]
                    auth_times=[]; original_lookup=app.state.security_runtime.lookup
                    async def lookup(digest):
                        began=time.perf_counter()
                        try: return await original_lookup(digest)
                        finally: auth_times.append((time.perf_counter()-began)*1000)
                    app.state.security_runtime.lookup=lookup
                    async def voice(path, raw, ingestion=None):
                        async with websocket(certificates,port,path,credential) as ws:
                            await ws.send(json.dumps(_start_message(ingestion) if ingestion else start()))
                            assert json.loads(await ws.recv())['type']=='session.ready'
                            data=pcm(raw)
                            for offset in range(0,len(data),6400):
                                await ws.send(data[offset:offset+6400]); await asyncio.sleep(.2)
                            stopped=time.perf_counter()
                            await ws.send(json.dumps(_stop_message() if ingestion else stop()))
                            events=[json.loads(await asyncio.wait_for(ws.recv(),240)) for _ in range(3)]
                            assert events[-1]['type']==('memory.saved' if ingestion else 'assistant.completed')
                            return events, round(time.perf_counter()-stopped,3)
                    events,cold = await measured('note_real_whisper_pg_bge',lambda:voice('/ws/memory',raw_note,uuid4()))
                    report['note_stop_to_saved_seconds']=cold
                    async with factory() as db:
                        rows=(await db.execute(sa.text('SELECT id,content FROM memories'))).all()
                        assert len(rows)==1 and 'dragon' in rows[0][1].lower() and 'venus' in rows[0][1].lower()
                        memory_id=str(rows[0][0])
                        assert await db.scalar(sa.text('SELECT count(*) FROM kulai_vector_records'))==1
                        assert await db.scalar(sa.text('SELECT vector_dims(embedding) FROM kulai_vector_records'))==1024
                    def grounded(value):
                        assert value['status']=='answered' and value['citations']
                        assert [c['memory_id'] for c in value['citations']]==[memory_id]
                        assert any(word in value['answer'].lower() for word in ('venus','wenus'))
                    async with client(certificates,port) as transport:
                        async def ask():
                            response=await transport.post('/api/assistant/ask',headers=headers(credential),
                                json={'query':'Where does the green dragon currently live?'},timeout=240)
                            assert response.status_code==200
                            grounded(response.json())
                        await measured('ask_first_request_existing_gpu_model',ask)
                        await measured('ask_warm',ask)
                        async def paced_during_ai():
                            async with websocket(certificates,port,'/ws/memory',credential) as ws:
                                await ws.send(json.dumps(_start_message(uuid4())))
                                assert json.loads(await ws.recv())['type']=='session.ready'
                                for _ in range(30):
                                    await ws.send(b'\0'*6400); await asyncio.sleep(.2)
                                ws.transport.abort()  # This secondary NOTE must never persist.
                        await measured('ai_with_concurrent_pcm',lambda:asyncio.gather(ask(),paced_during_ai()))
                        for language,answer in [('pl','Zielony smok mieszka na Wenus.'),('en','The green dragon lives on Venus.')]:
                            async def speech(answer=answer,language=language):
                                response=await transport.post('/api/assistant/speech-plan',headers=headers(credential),json={'answer':answer},timeout=240)
                                assert response.status_code==200
                                plan=response.json()
                                assert plan['segments']
                                assert all(s['language']==language for s in plan['segments'])
                            await measured('speech_plan_'+language,speech)
                    events,warm = await measured('ask_by_voice_real_whisper_bge_qwen',lambda:voice('/ws/assistant',raw_question))
                    grounded(events[-1]['payload'])
                    report['voice_question_stop_to_answer_seconds']=warm
                    report['auth_during_ai']=percentiles(auth_times)
                    async with factory() as db:
                        assert await db.scalar(sa.text('SELECT count(*) FROM memories'))==1
                        assert await db.scalar(sa.text('SELECT count(*) FROM kulai_vector_records'))==1
                    report['saved_memories']=report['saved_vectors']=1
                    for task in probes:
                        if task.done(): task.result()
                        task.cancel()
                    await asyncio.gather(*probes,return_exceptions=True)
                    report['authenticated_http_heartbeat_ms']=percentiles(heartbeat_times)
                    report['event_loop_tick_lateness_ms']=percentiles(tick_delays)
                assert all(not path.exists() for path in paths)
                report['temporary_transcription_audio_removed']=True
                report['models']=dict(whisper='large-v3/CUDA/int8_float16',embedding='bge-m3:567m-fp16/1024',llm='qwen3.5:9b')
                report['cold_start_scope']='Fresh Whisper provider first inference; Qwen/BGE daemon may already be resident, no forced unload'
        report['result']='PASS'
    except Exception as error:
        import traceback
        report['error_type']=type(error).__name__
        report['error_locations']=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)]
    finally:
        for task in probes: task.cancel()
        await asyncio.gather(*probes,return_exceptions=True)
        if monitor:
            monitor.cancel(); await asyncio.gather(monitor,return_exceptions=True)
        report['main_after']=asdict(await database_snapshot_url(database_config().async_url))
        report['main_unchanged']=report['main_before']==report['main_after']
        if not report['main_unchanged']: report['result']='FAIL'
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        with output.open('x',encoding='utf-8') as stream: json.dump(report,stream,indent=2)
        print(json.dumps(dict(result=report['result'],main_unchanged=report['main_unchanged'],error_type=report.get('error_type'),locations=report.get('error_locations'))),flush=True)
    return report['result']=='PASS'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',type=Path,required=True)
    logging.disable(logging.INFO)
    raise SystemExit(0 if asyncio.run(asyncio.wait_for(run(parser.parse_args().output),900)) else 1)
