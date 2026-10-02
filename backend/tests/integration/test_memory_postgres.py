from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from kulai_db import build_db_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kulai_memory.application import MemoryService
from kulai_memory.database_safety import (
    database_config,
    database_host_is_loopback,
    expected_alembic_heads,
)
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.settings import get_settings


def _require_opt_in() -> None:
    if os.environ.get("KULAI_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_POSTGRES_INTEGRATION=1 to use real PostgreSQL.")
    if get_settings().app_env.lower() not in {"dev", "development", "local", "test"}:
        pytest.fail("PostgreSQL integration tests require a non-production APP_ENV.")
    if not database_host_is_loopback(database_config()):
        pytest.fail("PostgreSQL integration tests require a loopback database host.")


async def _assert_database_is_at_expected_head(session_factory) -> None:
    heads = expected_alembic_heads()
    assert len(heads) == 1
    async with session_factory() as session:
        revisions = tuple(
            (
                await session.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                )
            )
            .scalars()
            .all()
        )
    assert revisions == heads


async def _round_trip_and_rollback() -> None:
    config = build_db_config(get_settings())
    engine = create_async_engine(config.async_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first_id = None
    second_id = None
    try:
        await _assert_database_is_at_expected_head(session_factory)
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                service = MemoryService(
                    repository=PostgresMemoryRepository(db=session)
                )
                first_session = uuid4()
                second_session = uuid4()
                first = await service.create_memory(
                    content="integration memory one",
                    source_kind="voice",
                    session_id=first_session,
                    metadata={"sequence": 1, "tags": ["integration", "first"]},
                )
                second = await service.create_memory(
                    content="integration memory two",
                    source_kind="voice",
                    session_id=second_session,
                    metadata={"sequence": 2, "nested": {"verified": True}},
                )
                first_id = first.id
                second_id = second.id

                assert await service.get_memory(first.id) == first
                assert await service.get_memory(second.id) == second
                recent = await service.list_recent_memories(limit=100)
                returned = [memory for memory in recent if memory.id in {first.id, second.id}]
                expected = sorted(
                    (first, second),
                    key=lambda memory: (memory.created_at, memory.id),
                    reverse=True,
                )
                assert returned == expected
                assert first.session_id == first_session
                assert second.session_id == second_session
            finally:
                await transaction.rollback()

        async with session_factory() as verification_session:
            repository = PostgresMemoryRepository(db=verification_session)
            assert first_id is not None and second_id is not None
            assert await repository.get_by_id(first_id) is None
            assert await repository.get_by_id(second_id) is None
    finally:
        await engine.dispose()


async def _exception_rolls_back() -> None:
    config = build_db_config(get_settings())
    engine = create_async_engine(config.async_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    memory_id = None
    try:
        with pytest.raises(RuntimeError, match="intentional integration rollback"):
            async with session_factory() as session:
                async with session.begin():
                    service = MemoryService(
                        repository=PostgresMemoryRepository(db=session)
                    )
                    memory = await service.create_memory(
                        content="integration rollback sentinel",
                        session_id=uuid4(),
                        metadata={"rollback": True},
                    )
                    memory_id = memory.id
                    raise RuntimeError("intentional integration rollback")

        async with session_factory() as verification_session:
            assert memory_id is not None
            repository = PostgresMemoryRepository(db=verification_session)
            assert await repository.get_by_id(memory_id) is None
    finally:
        await engine.dispose()


def test_real_memory_repository_round_trip_rolls_back() -> None:
    _require_opt_in()
    asyncio.run(_round_trip_and_rollback())


def test_real_memory_repository_exception_rolls_back() -> None:
    _require_opt_in()
    asyncio.run(_exception_rolls_back())
