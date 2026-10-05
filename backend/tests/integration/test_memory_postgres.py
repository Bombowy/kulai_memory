from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from alembic import command
from kulai_transcription import TranscriptionResult
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kulai_memory.application import (
    MemoryIdempotencyConflictError,
    MemoryService,
    TranscriptMemoryIngestionService,
    TranscriptMemoryIngestionStatus,
)
from kulai_memory.database_safety import (
    async_database_url,
    create_owned_temporary_database,
    database_config,
    database_host_is_loopback,
    drop_owned_temporary_database,
    expected_alembic_heads,
)
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.settings import get_settings
from scripts import migrate


def _require_opt_in() -> None:
    if os.environ.get("KULAI_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_POSTGRES_INTEGRATION=1 to use real PostgreSQL.")
    if get_settings().app_env.lower() not in {"dev", "development", "local", "test"}:
        pytest.fail("PostgreSQL integration tests require a non-production APP_ENV.")
    if not database_host_is_loopback(database_config()):
        pytest.fail("PostgreSQL integration tests require a loopback database host.")


def _upgrade_database(url: str, revision: str) -> None:
    previous_url = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        config = migrate.alembic_config()
        assert migrate.configure_x_arguments(config)
        command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()


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


@asynccontextmanager
async def _owned_migrated_session_factory(
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    config = database_config()
    owned = await create_owned_temporary_database(kind="backup", config=config)
    url = async_database_url(database=owned.name, config=config)
    try:
        await asyncio.to_thread(_upgrade_database, url, "head")
        engine = create_async_engine(url)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            await _assert_database_is_at_expected_head(session_factory)
            yield session_factory
        finally:
            await engine.dispose()
    finally:
        await drop_owned_temporary_database(owned, config=config)


def _ingestion_service(session: AsyncSession) -> TranscriptMemoryIngestionService:
    return TranscriptMemoryIngestionService(
        memory_service=MemoryService(
            repository=PostgresMemoryRepository(db=session)
        )
    )


async def _round_trip_and_rollback() -> None:
    first_id = None
    second_id = None
    async with _owned_migrated_session_factory() as session_factory:
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


async def _exception_rolls_back() -> None:
    memory_id = None
    async with _owned_migrated_session_factory() as session_factory:
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


async def _idempotent_ingestion_and_rollback() -> None:
    async with _owned_migrated_session_factory() as session_factory:
        async with session_factory() as count_session:
            before = await count_session.scalar(text("SELECT count(*) FROM memories"))
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                ingestion = _ingestion_service(session)
                ingestion_id = uuid4()
                session_id = uuid4()
                transcription = TranscriptionResult(
                    text="integration idempotent transcript",
                    provider_id="integration-fake",
                    model_id="large-v3",
                )

                created = await ingestion.ingest(
                    transcription=transcription,
                    ingestion_id=ingestion_id,
                    session_id=session_id,
                )
                duplicate = await ingestion.ingest(
                    transcription=transcription,
                    ingestion_id=ingestion_id,
                    session_id=session_id,
                )

                assert created.status is TranscriptMemoryIngestionStatus.CREATED
                assert duplicate.status is TranscriptMemoryIngestionStatus.DUPLICATE
                assert created.memory is not None
                assert duplicate.memory == created.memory
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM memories "
                            "WHERE ingestion_id = :ingestion_id"
                        ),
                        {"ingestion_id": ingestion_id},
                    )
                    == 1
                )

                with pytest.raises(MemoryIdempotencyConflictError):
                    await ingestion.ingest(
                        transcription=transcription.model_copy(
                            update={"text": "conflicting transcript"}
                        ),
                        ingestion_id=ingestion_id,
                        session_id=session_id,
                    )

                count = await session.scalar(text("SELECT count(*) FROM memories"))
                assert count == before + 1
            finally:
                await transaction.rollback()

        async with session_factory() as verification_session:
            assert (
                await verification_session.scalar(text("SELECT count(*) FROM memories"))
                == before
            )


async def _durable_idempotency_after_commit() -> None:
    async with _owned_migrated_session_factory() as session_factory:
        ingestion_id = uuid4()
        session_id = uuid4()
        transcription = TranscriptionResult(
            text="durable idempotent transcript",
            provider_id="integration-first",
            model_id="large-v3",
            duration_seconds=1.25,
        )

        async with session_factory() as first_session:
            first = await _ingestion_service(first_session).ingest(
                transcription=transcription,
                ingestion_id=ingestion_id,
                session_id=session_id,
            )
            assert first.status is TranscriptMemoryIngestionStatus.CREATED
            assert first.memory is not None
            memory_id = first.memory.id
            first_metadata = first.memory.metadata
            await first_session.commit()

        async with session_factory() as second_session:
            duplicate = await _ingestion_service(second_session).ingest(
                transcription=transcription.model_copy(
                    update={
                        "provider_id": "integration-retry",
                        "duration_seconds": 2.5,
                    }
                ),
                ingestion_id=ingestion_id,
                session_id=session_id,
            )
            assert duplicate.status is TranscriptMemoryIngestionStatus.DUPLICATE
            assert duplicate.memory is not None
            assert duplicate.memory.id == memory_id
            assert duplicate.memory.metadata == first_metadata
            await second_session.commit()

        async with session_factory() as conflict_session:
            with pytest.raises(MemoryIdempotencyConflictError):
                await _ingestion_service(conflict_session).ingest(
                    transcription=transcription.model_copy(
                        update={"text": "conflicting durable transcript"}
                    ),
                    ingestion_id=ingestion_id,
                    session_id=session_id,
                )
            await conflict_session.rollback()

        async with session_factory() as verification_session:
            await verification_session.execute(text("SET TRANSACTION READ ONLY"))
            stored = (
                await verification_session.execute(
                    text(
                        "SELECT id, content, source_kind, session_id, metadata_json "
                        "FROM memories WHERE ingestion_id = :ingestion_id"
                    ),
                    {"ingestion_id": ingestion_id},
                )
            ).one()
            matching_count = await verification_session.scalar(
                text(
                    "SELECT count(*) FROM memories "
                    "WHERE ingestion_id = :ingestion_id"
                ),
                {"ingestion_id": ingestion_id},
            )
            total_count = await verification_session.scalar(
                text("SELECT count(*) FROM memories")
            )
            await verification_session.rollback()

        assert stored.id == memory_id
        assert stored.content == transcription.text
        assert stored.source_kind == "voice"
        assert stored.session_id == session_id
        assert stored.metadata_json == first_metadata
        assert matching_count == 1
        assert total_count == 1


async def _concurrent_duplicate_ingestion() -> None:
    async with _owned_migrated_session_factory() as session_factory:
        ingestion_id = uuid4()
        session_id = uuid4()
        transcription = TranscriptionResult(
            text="concurrent idempotent transcript",
            provider_id="integration-concurrent",
            model_id="large-v3",
        )
        start_barrier = asyncio.Barrier(2)

        async def worker():
            async with session_factory() as session:
                ingestion = _ingestion_service(session)
                await start_barrier.wait()
                result = await ingestion.ingest(
                    transcription=transcription,
                    ingestion_id=ingestion_id,
                    session_id=session_id,
                )
                await session.commit()
                return result

        first, second = await asyncio.wait_for(
            asyncio.gather(worker(), worker()),
            timeout=15.0,
        )

        assert {first.status, second.status} == {
            TranscriptMemoryIngestionStatus.CREATED,
            TranscriptMemoryIngestionStatus.DUPLICATE,
        }
        assert first.memory is not None
        assert second.memory is not None
        assert first.memory.id == second.memory.id

        async with session_factory() as verification_session:
            await verification_session.execute(text("SET TRANSACTION READ ONLY"))
            stored = (
                await verification_session.execute(
                    text(
                        "SELECT id, content, session_id FROM memories "
                        "WHERE ingestion_id = :ingestion_id"
                    ),
                    {"ingestion_id": ingestion_id},
                )
            ).one()
            matching_count = await verification_session.scalar(
                text(
                    "SELECT count(*) FROM memories "
                    "WHERE ingestion_id = :ingestion_id"
                ),
                {"ingestion_id": ingestion_id},
            )
            total_count = await verification_session.scalar(
                text("SELECT count(*) FROM memories")
            )
            await verification_session.rollback()

        assert stored.id == first.memory.id
        assert stored.content == transcription.text
        assert stored.session_id == session_id
        assert matching_count == 1
        assert total_count == 1


async def _migration_backfills_existing_memory() -> None:
    config = database_config()
    owned = await create_owned_temporary_database(kind="backup", config=config)
    url = async_database_url(database=owned.name, config=config)
    memory_id = uuid4()
    try:
        await asyncio.to_thread(_upgrade_database, url, "kulai_memory_0001")
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO memories (
                            id, content, source_kind, session_id, metadata_json
                        ) VALUES (
                            :id, :content, 'voice', NULL, '{}'::jsonb
                        )
                        """
                    ),
                    {"id": memory_id, "content": "pre-ingestion migration row"},
                )
        finally:
            await engine.dispose()

        await asyncio.to_thread(_upgrade_database, url, "head")
        verification_engine = create_async_engine(url)
        try:
            async with verification_engine.connect() as connection:
                ingestion_id = await connection.scalar(
                    text("SELECT ingestion_id FROM memories WHERE id = :id"),
                    {"id": memory_id},
                )
                unique_constraint = await connection.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'uq_memories_ingestion_id'
                              AND contype = 'u'
                        )
                        """
                    )
                )
                revision = await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                await connection.rollback()
        finally:
            await verification_engine.dispose()

        assert ingestion_id == memory_id
        assert unique_constraint is True
        assert revision == "kulai_memory_0002"
    finally:
        await drop_owned_temporary_database(owned, config=config)


def test_real_memory_repository_round_trip_rolls_back() -> None:
    _require_opt_in()
    asyncio.run(_round_trip_and_rollback())


def test_real_memory_repository_exception_rolls_back() -> None:
    _require_opt_in()
    asyncio.run(_exception_rolls_back())


def test_real_idempotent_transcript_ingestion_rolls_back() -> None:
    _require_opt_in()
    asyncio.run(_idempotent_ingestion_and_rollback())


def test_real_idempotency_survives_commit_and_new_sessions() -> None:
    _require_opt_in()
    asyncio.run(_durable_idempotency_after_commit())


def test_real_concurrent_duplicate_ingestion_is_atomic() -> None:
    _require_opt_in()
    asyncio.run(_concurrent_duplicate_ingestion())


def test_real_migration_backfills_existing_memory_in_owned_database() -> None:
    _require_opt_in()
    asyncio.run(_migration_backfills_existing_memory())
