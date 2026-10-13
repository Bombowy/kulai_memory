"""Private stdin/stdout test IPC. Never accept a main DB; never print credentials."""
import asyncio
import json
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from uuid import UUID

from kulai_memory.database_safety import (
    database_config, database_config_for_database, require_owned_database, OwnedTemporaryDatabase,
)
from kulai_memory.security import CredentialManager, SecurityError
from kulai_memory.security_persistence.postgres import PostgresSecurityStore
from kulai_memory.security_persistence.pairing import PostgresPairingStore
from kulai_memory.pairing.domain import PairingService, PairingError
from kulai_memory.security.authentication import utc_now


async def run():
    data = json.loads(sys.stdin.readline())
    owned = OwnedTemporaryDatabase(data["database"], data["marker"], "backup")
    config = database_config()
    await require_owned_database(owned, config=config)
    target = database_config_for_database(owned.name, config=config)
    engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
    try:
        manager = CredentialManager(PostgresSecurityStore(async_sessionmaker(engine, expire_on_commit=False)))
        print("READY", flush=True)
        if sys.stdin.readline().strip() != "GO":
            raise SecurityError()
        try:
            action = data["operation"]
            if action == "rotate":
                result = await manager.rotate(data["token"], device_id=UUID(data["device_id"]))
                output = {"ok": True, "token_id": str(result.token_id)}
            elif action == "revoke":
                await manager.revoke(UUID(data["token_id"]))
                output = {"ok": True}
            elif action == "issue":
                result = await manager.issue(UUID(data["device_id"]))
                output = {"ok": True, "token_id": str(result.token_id)}
            elif action == "revoke_device":
                await manager.revoke_device(UUID(data["device_id"]))
                output = {"ok": True}
            elif action == 'pairing_claim':
                pairing = PairingService(PostgresPairingStore(async_sessionmaker(engine, expire_on_commit=False)), clock=utc_now)
                result = await pairing.claim(UUID(data['session_id']), data['proof'])
                output = {'ok': True, 'token_id': str(result.token_id)}
            elif action == 'pairing_request':
                pairing = PairingService(PostgresPairingStore(async_sessionmaker(engine, expire_on_commit=False)), clock=utc_now)
                result = await pairing.request(UUID(data['session_id']), data['invitation'], data['proof'], UUID(data['client_id']), 'Android test')
                output = {'ok': True, 'device_id': str(result.device_id)}
            else:
                raise SecurityError()
        except (SecurityError, PairingError):
            output = {"ok": False}
        print(json.dumps(output), flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception:
        print('{"ok":false,"stage":"worker"}', flush=True)
        raise SystemExit(1)
