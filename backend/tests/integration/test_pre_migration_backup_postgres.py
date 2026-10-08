from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from uuid import uuid4

import pytest
from kulai_embeddings import EmbeddingVector
from kulai_vector_store import VectorRecord, VectorUpsertRequest
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.integration.test_memory_postgres import _require_opt_in, _upgrade_database
from kulai_memory.application import MEMORY_VECTOR_NAMESPACE
from kulai_memory.database_safety import (
    DatabaseSafetyError, PRE_MIGRATION_FAILURES, async_database_url,
    create_owned_temporary_database, database_config, database_exists,
    drop_owned_temporary_database, run_database_doctor,
)
from scripts import db_backup, db_restore_smoke


async def _exercise(tmp_path, monkeypatch):
    config = database_config()
    source = await create_owned_temporary_database(kind="backup", config=config)
    url = async_database_url(database=source.name, config=config)
    observed_restores = []
    original_doctor = db_restore_smoke.run_database_doctor

    async def observe_doctor(*, async_url):
        report = await original_doctor(async_url=async_url)
        if async_url != url:
            fingerprints = await db_restore_smoke.fingerprint_url(async_url)
            observed_restores.append((report, fingerprints))
        return report

    monkeypatch.setattr(db_restore_smoke, "run_database_doctor", observe_doctor)
    try:
        await asyncio.to_thread(_upgrade_database, url, "kulai_memory_0002")
        engine = create_async_engine(url)
        try:
            factory = async_sessionmaker(engine)
            async with factory() as session:
                async with session.begin():
                    for index in (1, 2):
                        memory_id = uuid4()
                        # The current repository intentionally requires the 0003 tombstone table.
                        # These legacy-state fixtures are inserted only into this owned 0002 DB.
                        await session.execute(text(
                            "INSERT INTO memories (id, ingestion_id, content, source_kind, metadata_json) "
                            "VALUES (:id, :ingestion_id, :content, 'voice', '{}'::jsonb)"
                        ), {"id": memory_id, "ingestion_id": uuid4(), "content": f"synthetic pre-migration memory {index}"})
                        await PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)).upsert(
                            VectorUpsertRequest(namespace=MEMORY_VECTOR_NAMESPACE, records=(VectorRecord(
                                id=str(memory_id), vector=EmbeddingVector(values=(float(index),) * 1024),
                                metadata={"source_memory_id": str(memory_id), "synthetic": True},
                            ),)),
                        )
        finally:
            await engine.dispose()

        before = await db_restore_smoke.fingerprint_url(url)
        assert before[0].count == before[1].count == 2
        pending = await run_database_doctor(async_url=url)
        assert not pending.ok
        assert {c.name for c in pending.checks if not c.ok} == PRE_MIGRATION_FAILURES

        # Default mode still refuses this legacy source; no archive is produced.
        strict_blocked = tmp_path / "strict_blocked.dump"
        with pytest.raises(DatabaseSafetyError):
            await db_backup.create_owned_database_backup(
                strict_blocked, force=False, owned=source, config=config,
            )
        assert not strict_blocked.exists()

        archive = tmp_path / "owned_0002.dump"
        size, digest, _ = await db_backup.create_owned_database_backup(
            archive, force=False, owned=source, config=config,
            pre_migration_from="kulai_memory_0002",
        )
        assert size > 0 and digest == db_backup.sha256_file(archive)

        # Even a valid 0002 archive is rejected by the default restore doctor.
        with pytest.raises(DatabaseSafetyError):
            await db_restore_smoke.restore_owned_database_backup(archive, owned=source, config=config)
        assert not observed_restores[-1][0].ok

        restored_name, memories, vectors = await db_restore_smoke.restore_owned_database_backup(
            archive, owned=source, config=config, pre_migration_from="kulai_memory_0002",
        )
        assert memories == vectors == 2
        assert not await database_exists(restored_name, config=config)
        restored_doctor, restored_fingerprints = observed_restores[-1]
        checks = {c.name: c for c in restored_doctor.checks}
        assert checks["alembic.current"].value == ["kulai_memory_0002"]
        assert {c.name for c in restored_doctor.checks if not c.ok} == PRE_MIGRATION_FAILURES
        assert restored_fingerprints == before
        assert await db_restore_smoke.fingerprint_url(url) == before
        assert archive.is_file()
        print(json.dumps({
            "owned_0002": "PASS", "restored_revision": "kulai_memory_0002",
            "backup_size": size, "backup_sha256": digest,
            "memory_source_restored": asdict(before[0]), "vector_source_restored": asdict(before[1]),
            "source_unchanged": True, "restore_target_removed": True,
        }))

        # Only the owned fixture is upgraded. Main is never a migration target.
        await asyncio.to_thread(_upgrade_database, url, "kulai_memory_0003")
        assert (await run_database_doctor(async_url=url)).ok
        assert await db_restore_smoke.fingerprint_url(url) == before
        strict_archive = tmp_path / "owned_0003.dump"
        await db_backup.create_owned_database_backup(strict_archive, force=False, owned=source, config=config)
        restored_name, memories, vectors = await db_restore_smoke.restore_owned_database_backup(
            strict_archive, owned=source, config=config,
        )
        strict_report, strict_fingerprints = observed_restores[-1]
        assert strict_report.ok and strict_fingerprints == before
        assert next(c.value for c in strict_report.checks if c.name == "alembic.current") == ["kulai_memory_0003"]
        assert memories == vectors == 2
        assert await db_restore_smoke.fingerprint_url(url) == before
        assert not await database_exists(restored_name, config=config)
        print("owned_0003.strict_backup=PASS;strict_restore=PASS;fingerprints_match=true;source_unchanged=true")
    finally:
        await drop_owned_temporary_database(source, config=config)
    assert not await database_exists(source.name, config=config)
    print("owned_source.cleanup=PASS")


def test_owned_0002_pre_migration_backup_restore_and_0003_strict_mode(tmp_path, monkeypatch):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_exercise(tmp_path, monkeypatch), timeout=180))
