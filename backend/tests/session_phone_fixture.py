"""Explicit physical smoke fixture. Synthetic providers + process-owned DB; loopback only.

PL/EN: trusted local file IPC is test administration, never a public admin endpoint.
Run with KULAI_RUN_POSTGRES_INTEGRATION=1; finish explicitly to drop the owned database.
"""
import asyncio
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import tempfile
from uuid import UUID
import sqlalchemy as sa
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_security_uvicorn import server
from backend.tests.test_security_transport import Runtime
from kulai_memory.app_factory import create_app
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime
from kulai_memory.security_persistence.sessions import PostgresSessionStore
from kulai_memory.pairing.domain import PairingService
from kulai_memory.security.authentication import utc_now
from kulai_memory.security import Action, CredentialManager


async def run():
    control=Path(tempfile.mkdtemp(prefix='kulai-4d3a-phone-'))
    print('CONTROL='+str(control),flush=True)
    try:
        async with owned_store() as (_,_,target,factory,_):
            store=PostgresSessionStore(factory); pairing=PairingService(store,clock=utc_now)
            runtime=Runtime()
            app=create_app(voice_runtime_factory=lambda:runtime,
                security_runtime_factory=lambda:PostgresSecurityRuntime(config_factory=lambda:target))
            async with server(app) as port:
                print('PORT='+str(port),flush=True)
                while True:
                    command=control/'command.json'
                    if not command.exists(): await asyncio.sleep(.25); continue
                    data=json.loads(command.read_text(encoding='utf-8-sig')); command.unlink()
                    name=data['operation']
                    if name=='finish': break
                    if name=='invite':
                        invitation=await pairing.invite(actions=frozenset(Action),token_ttl=timedelta(seconds=60))
                        result={'session_id':str(invitation.session_id),'invitation_secret':invitation.secret,
                            'expires_at':invitation.expires_at.isoformat()}
                    elif name=='pending': result=[asdict(v) for v in await store.pending()]
                    elif name=='approve':
                        view=next(v for v in await store.pending() if str(v.session_id)==data['session_id'] and str(v.device_id)==data['device_id'])
                        assert view.verification_code==data['verification_code']
                        await store.decide(view.session_id,view.device_id,approve=True); result={'approved':True}
                    elif name=='revoke':
                        await CredentialManager(store).revoke_device(UUID(data['device_id'])); result={'revoked':True}
                    elif name=='statistics':
                        async with factory() as session:
                            families=[dict(row) for row in (await session.execute(sa.text(
                                'SELECT id,device_id,generation,revoked_at FROM public.kulai_security_sessions ORDER BY id'))).mappings()]
                            counts={table:await session.scalar(sa.text(f'SELECT count(*) FROM public.{table}'))
                                for table in ('kulai_security_refreshes','kulai_security_tokens','memories','memory_ingestion_tombstones')}
                        result={'families':families,'counts':counts,'private_calls':runtime.private_calls}
                    else: raise ValueError()
                    temporary=control/'result.tmp'
                    temporary.write_text(json.dumps(result,default=str),encoding='utf-8'); temporary.replace(control/'result.json')
                    print('COMPLETE='+name,flush=True)
        print('OWNED_DATABASE_DROPPED',flush=True)
    finally:
        for name in ('result.json','command.json','result.tmp'): (control/name).unlink(missing_ok=True)
        control.rmdir()


if __name__=='__main__':
    try: asyncio.run(run())
    except Exception:
        print('Physical fixture failed safely.',flush=True)
        raise SystemExit(1)
