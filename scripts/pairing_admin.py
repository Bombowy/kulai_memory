"""Trusted local administration. No public admin API or long-lived secret output."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import secrets
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/src'))
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from kulai_memory.database_safety import database_config, database_host_is_loopback, run_database_doctor
from kulai_memory.settings import get_settings
from kulai_memory.security import Action, CredentialManager
from kulai_memory.security.authentication import utc_now
from kulai_memory.pairing.domain import PairingService, PairingError
from kulai_memory.security_persistence.pairing import PostgresPairingStore
from kulai_memory.security_persistence.integrity import inspect_security_schema
from kulai_memory.security_persistence.schema_v2 import SECURITY_HEAD


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    invite = commands.add_parser('invite')
    invite.add_argument('--show-secret', action='store_true', required=True)
    invite.add_argument('--permissions', nargs='+', choices=[a.value for a in Action], required=True)
    invite.add_argument('--token-ttl-seconds', type=int, default=3600)
    commands.add_parser('pending')
    commands.add_parser('devices')
    prune = commands.add_parser('prune-expired')
    prune.add_argument('--confirm', action='store_true', required=True)
    for name in ('approve', 'reject'):
        decision = commands.add_parser(name)
        decision.add_argument('--session-id', type=UUID, required=True)
        decision.add_argument('--device-id', type=UUID, required=True)
        decision.add_argument('--verification-code', required=name == 'approve')
    revoke = commands.add_parser('revoke')
    revoke.add_argument('--device-id', type=UUID, required=True)
    return result


async def run(args):
    config = database_config()
    if not database_host_is_loopback(config) or get_settings().app_env.lower() not in {'dev', 'development', 'local', 'test'}:
        raise PairingError()
    if not (await run_database_doctor(async_url=config.async_url, require_security=True)).ok:
        raise PairingError()
    engine = create_async_engine(config.async_url, echo=False, hide_parameters=True,
        connect_args={'server_settings': {'statement_timeout': '4000', 'lock_timeout': '3000', 'search_path': 'pg_catalog,public'}})
    try:
        async with engine.connect() as connection:
            state = await inspect_security_schema(connection)
            if not state.valid or state.revision != SECURITY_HEAD:
                raise PairingError()
        store = PostgresPairingStore(async_sessionmaker(engine, expire_on_commit=False))
        service = PairingService(store, clock=utc_now)
        if args.command == 'invite':
            invite = await service.invite(actions=frozenset(Action(a) for a in args.permissions),
                token_ttl=timedelta(seconds=args.token_ttl_seconds))
            # Only this explicit administrative operation reveals the one-time secret.
            print(json.dumps({'session_id': str(invite.session_id), 'invitation_secret': invite.secret,
                'expires_at': invite.expires_at.isoformat()}))
        elif args.command == 'pending':
            print(json.dumps([asdict(view) for view in await store.pending()], default=str))
        elif args.command == 'devices':
            print(json.dumps([str(device) for device in await store.trusted_devices()]))
        elif args.command == 'prune-expired':
            print('Expired terminal sessions removed:', await store.prune_expired())
        elif args.command in ('approve', 'reject'):
            if args.command == 'approve':
                pending = next((v for v in await store.pending() if v.session_id == args.session_id and v.device_id == args.device_id), None)
                if pending is None or not secrets.compare_digest(pending.verification_code or '', args.verification_code):
                    raise PairingError()
            await store.decide(args.session_id, args.device_id, approve=args.command == 'approve')
            print('Pairing decision recorded.')
        else:
            await CredentialManager(store).revoke_device(args.device_id)
            print('Device access revoked.')
        return 0
    finally:
        await engine.dispose()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception:
        print('Pairing administration unavailable or operation refused.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
