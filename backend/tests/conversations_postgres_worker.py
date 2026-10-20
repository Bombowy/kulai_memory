"""New-process read proof, accepting an owned test database capability on stdin."""

import asyncio
import json
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kulai_memory.conversation_persistence import PostgresConversationRepository
from kulai_memory.database_safety import (
    OwnedTemporaryDatabase, database_config, database_config_for_database, require_owned_database,
)


async def read(data):
    config = database_config()
    owned = OwnedTemporaryDatabase(name=data["database"], marker=data["marker"], kind="backup")
    await require_owned_database(owned, config=config)
    target = database_config_for_database(owned.name, config=config)
    engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
    try:
        repository = PostgresConversationRepository(async_sessionmaker(engine, expire_on_commit=False))
        page = await repository.recent_messages(conversation_id=UUID(data["conversation_id"]))
        return page.model_dump(mode="json")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        result = asyncio.run(read(json.loads(sys.stdin.readline())))
        print(json.dumps(result, ensure_ascii=True))
    except Exception:
        print("Owned conversation read failed.", file=sys.stderr)
        sys.exit(1)
