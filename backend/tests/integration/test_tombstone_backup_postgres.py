from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.integration.test_memory_postgres import _require_opt_in
from kulai_memory.application import Memory, MemoryIngestionRetiredError
from kulai_memory.database_safety import (
    DatabaseSafetyError, async_database_url, create_owned_temporary_database,
    database_config, database_exists, database_snapshot_url, drop_owned_temporary_database,
    restore_archive_to_owned_database, run_database_doctor,
)
from kulai_memory.persistence import PostgresMemoryRepository
from scripts import db_backup, db_backup_restore_drill as drill, db_restore_smoke


@asynccontextmanager
async def source_fixture():
    config = database_config()
    owned = await create_owned_temporary_database(kind="backup", config=config)
    try:
        await drill.migrate_owned_database(owned, config=config)
        fixtures = await drill.seed_owned_source(owned, config=config, dimension=1024)
        yield owned, config, async_database_url(database=owned.name, config=config), fixtures
    finally:
        await drop_owned_temporary_database(owned, config=config)
    assert not await database_exists(owned.name, config=config)


async def behavioral_round_trip(tmp_path):
    async with source_fixture() as (source, config, url, fixtures):
        before = await database_snapshot_url(url)
        assert before.memories.count == 2 and before.vectors.count == 1 and before.tombstones.count == 1
        archive = tmp_path / "tombstone.dump"
        size, digest, _ = await db_backup.create_owned_database_backup(
            archive, force=False, owned=source, config=config,
        )
        result = await db_restore_smoke.restore_owned_database_backup(archive, owned=source, config=config)
        assert result.snapshot == before
        assert not await database_exists(result.database, config=config)
        assert await database_snapshot_url(url) == before
        assert archive.is_file()

        # A separate owned restore is kept alive for controlled behavioral writes.
        restored = await create_owned_temporary_database(kind="restore", config=config)
        try:
            await restore_archive_to_owned_database(archive, restored, config=config)
            restored_url = async_database_url(database=restored.name, config=config)
            assert (await run_database_doctor(async_url=restored_url)).ok
            assert await database_snapshot_url(restored_url) == before
            engine = create_async_engine(restored_url)
            try:
                factory = async_sessionmaker(engine, expire_on_commit=False)
                for content in (fixtures.retired_memory.content, "different synthetic replay content"):
                    replay = fixtures.retired_memory.model_copy(update={"content": content})
                    async with factory() as session:
                        with pytest.raises(MemoryIngestionRetiredError):
                            async with session.begin():
                                await PostgresMemoryRepository(db=session).create_or_get_by_ingestion_id(replay)
                    assert await database_snapshot_url(restored_url) == before
                async with factory() as session:
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                    assert await session.scalar(text(
                        "SELECT count(*) FROM memories WHERE ingestion_id = :id"
                    ), {"id": fixtures.retired_memory.ingestion_id}) == 0
                    await session.rollback()
                fresh = Memory(content="synthetic fresh ingestion after restore")
                async with factory() as session:
                    async with session.begin():
                        created = await PostgresMemoryRepository(db=session).create_or_get_by_ingestion_id(fresh)
                        assert created.created and created.memory.id == fresh.id
                async with factory() as session:
                    assert await PostgresMemoryRepository(db=session).get_by_id(fresh.id) == created.memory
                after_fresh = await database_snapshot_url(restored_url)
                assert after_fresh.memories.count == before.memories.count + 1
                assert after_fresh.vectors == before.vectors and after_fresh.tombstones == before.tombstones
            finally:
                await engine.dispose()
        finally:
            await drop_owned_temporary_database(restored, config=config)
        assert not await database_exists(restored.name, config=config)
        assert await database_snapshot_url(url) == before
        print(json.dumps({
            "owned_tombstone_restore": "PASS", "retired_replay": "RETIRED", "resurrected": 0,
            "fresh_ingestion": "CREATED", "fingerprints_match": True, "source_unchanged": True,
            "tombstones_source_restored": asdict(before.tombstones), "backup_size": size, "backup_sha256": digest,
        }))


async def tamper_round_trip(tmp_path, monkeypatch, field):
    async with source_fixture() as (source, config, url, _):
        baseline = await database_snapshot_url(url)
        archive = tmp_path / "tamper.dump"
        await db_backup.create_owned_database_backup(archive, force=False, owned=source, config=config)
        original = db_restore_smoke.restore_archive_to_owned_database
        targets = []
        async def restore_then_tamper(backup, owned, *, config):
            await original(backup, owned, config=config)
            targets.append(owned.name)
            target_url = async_database_url(database=owned.name, config=config)
            engine = create_async_engine(target_url)
            try:
                async with engine.begin() as connection:
                    # Static statements only; modifications apply to the owned restore target.
                    statements = {
                        "ingestion_id": "UPDATE memory_ingestion_tombstones SET ingestion_id = 'ffffffff-ffff-4fff-8fff-ffffffffffff'::uuid",
                        "memory_id": "UPDATE memory_ingestion_tombstones SET memory_id = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'::uuid",
                        "deleted_at": "UPDATE memory_ingestion_tombstones SET deleted_at = deleted_at + interval '1 microsecond'",
                    }
                    await connection.execute(text(statements[field]))
            finally:
                await engine.dispose()
            assert (await run_database_doctor(async_url=target_url)).ok
            tampered = await database_snapshot_url(target_url)
            assert tampered.memories == baseline.memories and tampered.vectors == baseline.vectors
            assert tampered.tombstones.count == baseline.tombstones.count
            assert tampered.tombstones.sha256 != baseline.tombstones.sha256
        monkeypatch.setattr(db_restore_smoke, "restore_archive_to_owned_database", restore_then_tamper)
        with pytest.raises(DatabaseSafetyError, match="fingerprints do not match") as caught:
            await db_restore_smoke.restore_owned_database_backup(archive, owned=source, config=config)
        assert "ffffffff" not in str(caught.value) and "eeeeeeee" not in str(caught.value)
        assert len(targets) == 1 and not await database_exists(targets[0], config=config)
        assert await database_snapshot_url(url) == baseline
        assert archive.is_file()
        print(f"owned_tombstone.tamper_field={field};rejected=true;count_unchanged=true;cleanup=PASS")


async def canonical_drill():
    main_url = database_config().async_url
    before = await database_snapshot_url(main_url)
    from kulai_memory.database_safety import expected_alembic_heads
    mode = before.revision[0] if before.revision != expected_alembic_heads() else None
    result = await drill.run_drill(pre_migration_from=mode)
    assert result.source_snapshot == result.restored_snapshot
    assert result.source_snapshot.tombstones.count == 1
    assert await database_snapshot_url(main_url) == before
    print("canonical.drill=PASS;counts=2/1/1;three_fingerprints_match=true;main_unchanged=true")


def test_owned_tombstone_restore_preserves_retired_replay_and_accepts_fresh_ingestion(tmp_path):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(behavioral_round_trip(tmp_path), timeout=180))


@pytest.mark.parametrize("field", ["ingestion_id", "memory_id", "deleted_at"])
def test_owned_tombstone_tamper_rejected_with_same_count(tmp_path, monkeypatch, field):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(tamper_round_trip(tmp_path, monkeypatch, field), timeout=180))


def test_canonical_drill_verifies_all_three_categories():
    _require_opt_in()
    asyncio.run(asyncio.wait_for(canonical_drill(), timeout=180))
