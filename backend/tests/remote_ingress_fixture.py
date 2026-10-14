"""Interactive synthetic mTLS fixture: temporary certificates and marker-owned DB.

Local command.json administration follows session_phone_fixture; no HTTP admin API.
Never emits bearer/refresh credentials to logs. Finish or Ctrl+C closes and cleans up.
"""
import asyncio
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import tempfile

from backend.tests.remote_tls_fixtures import Certificates, tls_server
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_remote_ingress_tls import application
from kulai_memory.pairing.domain import PairingService
from kulai_memory.security import Action
from kulai_memory.security.authentication import utc_now
from kulai_memory.security_persistence.sessions import PostgresSessionStore


async def run():
    with tempfile.TemporaryDirectory(prefix='kulai-synthetic-ingress-') as directory:
        control = Path(directory)
        certificates = Certificates(control)
        async with owned_store() as (_, _, target, factory, _):
            store = PostgresSessionStore(factory)
            pairing = PairingService(store, clock=utc_now)
            app, _ = application(target, factory, certificates.config())
            async with tls_server(app) as (port, server):
                print('CONTROL='+str(control), flush=True)
                print('PORT='+str(port), flush=True)
                print('HOST=memory.test', flush=True)
                print('CLIENT_CA='+str(certificates.path('ca','pem')), flush=True)
                print('GATEWAY_CERTIFICATE='+str(certificates.path('gateway','pem')), flush=True)
                print('GATEWAY_KEY_FILE='+str(certificates.path('gateway','key')), flush=True)
                while not server.should_exit:
                    command = control/'command.json'
                    if not command.exists():
                        await asyncio.sleep(.25)
                        continue
                    data = json.loads(command.read_text(encoding='utf-8-sig'))
                    command.unlink()
                    operation = data['operation']
                    if operation == 'finish':
                        break
                    if operation == 'invite':
                        invitation = await pairing.invite(actions=frozenset(Action),token_ttl=timedelta(seconds=60))
                        # Short-lived synthetic invitation only, never a bearer/refresh.
                        result = {'session_id':str(invitation.session_id),'invitation_secret':invitation.secret,
                                  'expires_at':invitation.expires_at.isoformat()}
                    elif operation == 'pending':
                        result = [asdict(item) for item in await store.pending()]
                    elif operation == 'approve':
                        match = next(item for item in await store.pending() if
                            str(item.session_id) == data['session_id'] and str(item.device_id) == data['device_id'])
                        if match.verification_code != data['verification_code']:
                            raise ValueError()
                        await store.decide(match.session_id, match.device_id, approve=True)
                        result = {'approved':True}
                    else:
                        raise ValueError()
                    temporary = control/'result.tmp'
                    temporary.write_text(json.dumps(result,default=str),encoding='utf-8')
                    temporary.replace(control/'result.json')
                    print('SYNTHETIC_COMMAND_COMPLETE',flush=True)
        print('OWNED_DATABASE_AND_TEMPORARY_PKI_CLEANED',flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        print('Synthetic ingress fixture failed safely.',flush=True)
        raise SystemExit(1)
