"""Run a non-empty backup/restore drill using owned temporary databases only."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from kulai_embeddings import EmbeddingVector
from kulai_vector_store import VectorRecord, VectorUpsertRequest
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_db import DbConfig
from kulai_memory.application import MEMORY_VECTOR_NAMESPACE, Memory, MemoryService
from kulai_memory.deletion_persistence import delete_memory
from kulai_memory.database_safety import (
    DatabaseSnapshot,
    OwnedTemporaryDatabase,
    async_database_url,
    create_owned_temporary_database,
    database_config,
    database_exists,
    database_snapshot_url,
    drop_owned_temporary_database,
    postgres_connection,
    require_owned_database,
    restore_archive_to_owned_database,
    run_database_doctor,
    safe_error_message,
    validate_restore_source,
)
from kulai_memory.persistence import PostgresMemoryRepository
from kulai_memory.settings import get_settings
from scripts.db_backup import create_owned_database_backup


@dataclass(frozen=True, slots=True)
class DrillFixtures:
    memories: tuple[Memory, Memory]
    retired_memory: Memory
    namespace: str
    record_id: str
    vector_values: tuple[float, ...]
    vector_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class DrillResult:
    source_database: str
    restore_database: str
    source_snapshot: DatabaseSnapshot
    restored_snapshot: DatabaseSnapshot
    backup_size: int
    backup_sha256: str
    pg_dump_version: str


def parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


async def database_snapshot(url: str) -> DatabaseSnapshot:
    return await database_snapshot_url(url)


async def migrate_owned_database(
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig,
) -> None:
    await require_owned_database(owned, config=config)
    environment = dict(os.environ)
    for key in (
        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME",
        "DATABASE_URL",
        "PGPASSWORD",
    ):
        environment.pop(key, None)
    environment["DATABASE_URL"] = async_database_url(
        database=owned.name,
        config=config,
    )
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "migrate.py"), "upgrade"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Temporary database migration failed with exit code "
            f"{completed.returncode}."
        )


def deterministic_vector(dimension: int) -> tuple[float, ...]:
    return tuple(float((index % 17) - 8) / 8.0 for index in range(dimension))


async def seed_owned_source(
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig,
    dimension: int,
) -> DrillFixtures:
    await require_owned_database(owned, config=config)
    url = async_database_url(database=owned.name, config=config)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    namespace = "kulai-memory-backup-drill"
    vector_values = deterministic_vector(dimension)
    vector_metadata: dict[str, object] = {
        "kind": "synthetic-backup-drill",
        "nested": {"language": "pl", "verified": True},
    }
    try:
        async with factory() as session:
            current_database = await session.scalar(text("SELECT current_database()"))
            if current_database != owned.name:
                raise RuntimeError("Refusing to seed an unexpected database.")
            await require_owned_database(owned, config=config)
            service = MemoryService(
                repository=PostgresMemoryRepository(db=session)
            )
            first = await service.create_memory(
                content="Zażółć gęślą jaźń — pamięć numer jeden 🇵🇱",
                session_id=uuid4(),
                metadata={
                    "language": "pl",
                    "nested": {"level": 1, "labels": ["żółty", "gęśl"]},
                },
            )
            second = await service.create_memory(
                content="Druga pamięć: Kraków, Łódź i Unicode ✓",
                session_id=uuid4(),
                metadata={
                    "language": "pl",
                    "nested": {"level": 2, "flags": {"restore": True}},
                },
            )
            if first.id == second.id or first.session_id == second.session_id:
                raise RuntimeError("Drill fixtures must use distinct identifiers.")
            if any(memory.created_at.utcoffset() is None for memory in (first, second)):
                raise RuntimeError("Drill Memory timestamps must be timezone-aware.")

            vector_store = PgVectorStore(
                db=session,
                config=PgVectorStoreConfig(dimension=dimension),
            )
            await vector_store.upsert(
                VectorUpsertRequest(
                    namespace=namespace,
                    records=(
                        VectorRecord(
                            id=str(first.id),
                            vector=EmbeddingVector(values=vector_values),
                            metadata=vector_metadata,
                        ),
                    ),
                )
            )
            retired = await service.create_memory(content="synthetic retired backup identity")
            await vector_store.upsert(VectorUpsertRequest(
                namespace=MEMORY_VECTOR_NAMESPACE,
                records=(VectorRecord(
                    id=str(retired.id), vector=EmbeddingVector(values=vector_values),
                ),),
            ))
            await require_owned_database(owned, config=config)
            await session.commit()
        await delete_memory(memory_id=retired.id, session_factory=factory)
        return DrillFixtures(
            memories=(first, second), retired_memory=retired,
            namespace=namespace, record_id=str(first.id),
            vector_values=vector_values, vector_metadata=vector_metadata,
        )
    finally:
        await engine.dispose()


async def verify_fixtures(
    url: str,
    fixtures: DrillFixtures,
    *,
    dimension: int,
) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            repository = PostgresMemoryRepository(db=session)
            restored_memories = tuple(
                [
                    await repository.get_by_id(memory.id)
                    for memory in fixtures.memories
                ]
            )
            if restored_memories != fixtures.memories:
                raise RuntimeError("Restored Memory fields do not match fixtures.")
            if await repository.get_by_id(fixtures.retired_memory.id) is not None:
                raise RuntimeError("Retired Memory unexpectedly exists in drill database.")
            retired_identity = (await session.execute(text(
                "SELECT memory_id FROM memory_ingestion_tombstones WHERE ingestion_id = :id"
            ), {"id": fixtures.retired_memory.ingestion_id})).scalar_one_or_none()
            if retired_identity != fixtures.retired_memory.id:
                raise RuntimeError("Restored tombstone identity does not match fixture.")
            if not all(
                isinstance(memory.id, UUID)
                and memory.session_id is not None
                and memory.created_at.utcoffset() is not None
                for memory in restored_memories
                if memory is not None
            ):
                raise RuntimeError("Restored Memory identifiers or timestamps are invalid.")

            row = (
                await session.execute(
                    text(
                        """
                        SELECT namespace_key, record_id, metadata_json,
                               embedding::text AS embedding_text,
                               vector_dims(embedding) AS embedding_dimension
                        FROM kulai_vector_records
                        WHERE namespace_key = :namespace AND record_id = :record_id
                        """
                    ),
                    {
                        "namespace": fixtures.namespace,
                        "record_id": fixtures.record_id,
                    },
                )
            ).one_or_none()
            if row is None:
                raise RuntimeError("Restored vector fixture is missing.")
            values = tuple(float(value) for value in json.loads(row.embedding_text))
            if (
                row.namespace_key != fixtures.namespace
                or row.record_id != fixtures.record_id
                or row.metadata_json != fixtures.vector_metadata
                or row.embedding_dimension != dimension
                or len(values) != dimension
            ):
                raise RuntimeError("Restored vector identity or dimension is invalid.")
            positions = (0, dimension // 2, dimension - 1)
            if any(
                not math.isclose(
                    values[position],
                    fixtures.vector_values[position],
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
                for position in positions
            ):
                raise RuntimeError("Restored vector controlled values do not match.")
            await session.rollback()
    finally:
        await engine.dispose()


async def run_drill() -> DrillResult:
    settings = get_settings()
    dimension = settings.kulai_vector_dimension
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
        raise ValueError("KULAI_VECTOR_DIMENSION must be a positive integer.")
    config = database_config()
    source_connection = validate_restore_source(config, app_env=settings.app_env)
    main_doctor_before = await run_database_doctor()
    if not main_doctor_before.ok:
        raise RuntimeError("Main database invariants failed before the drill.")
    main_before = await database_snapshot(config.async_url)

    source: OwnedTemporaryDatabase | None = None
    restored: OwnedTemporaryDatabase | None = None
    cleanup_errors: list[Exception] = []
    result: DrillResult | None = None
    try:
        source = await create_owned_temporary_database(kind="backup", config=config)
        await migrate_owned_database(source, config=config)
        source_url = async_database_url(database=source.name, config=config)
        source_doctor = await run_database_doctor(async_url=source_url)
        if not source_doctor.ok:
            raise RuntimeError("Temporary source database invariants failed.")
        fixtures = await seed_owned_source(
            source,
            config=config,
            dimension=dimension,
        )
        source_snapshot = await database_snapshot(source_url)
        if (
            source_snapshot.memories.count != 2 or source_snapshot.vectors.count != 1
            or source_snapshot.tombstones is None or source_snapshot.tombstones.count != 1
        ):
            raise RuntimeError("Temporary source fixture counts are invalid.")
        await verify_fixtures(source_url, fixtures, dimension=dimension)

        with tempfile.TemporaryDirectory(prefix="kulai_memory_backup_drill_") as temp:
            archive = Path(temp) / "drill.dump"
            backup_size, backup_sha256, pg_dump_version = (
                await create_owned_database_backup(
                    archive,
                    force=False,
                    owned=source,
                    config=config,
                )
            )
            restored = await create_owned_temporary_database(
                kind="restore",
                config=config,
            )
            await restore_archive_to_owned_database(
                archive,
                restored,
                config=config,
            )
            restored_url = async_database_url(database=restored.name, config=config)
            restored_doctor = await run_database_doctor(async_url=restored_url)
            if not restored_doctor.ok:
                raise RuntimeError("Restored database invariants failed.")
            restored_snapshot = await database_snapshot(restored_url)
            await verify_fixtures(restored_url, fixtures, dimension=dimension)
            if restored_snapshot != source_snapshot:
                raise RuntimeError("Restored database fingerprints do not match source.")
            if await database_snapshot(source_url) != source_snapshot:
                raise RuntimeError("Source database changed during drill restore verification.")
            result = DrillResult(
                source_database=source.name,
                restore_database=restored.name,
                source_snapshot=source_snapshot,
                restored_snapshot=restored_snapshot,
                backup_size=backup_size,
                backup_sha256=backup_sha256,
                pg_dump_version=pg_dump_version,
            )
    finally:
        for owned in (restored, source):
            if owned is None:
                continue
            try:
                await drop_owned_temporary_database(owned, config=config)
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise RuntimeError(
                "Owned temporary database cleanup failed; no unverified drop was used."
            ) from cleanup_errors[0]

    if result is None:
        raise RuntimeError("Backup/restore drill did not produce a result.")
    if await database_exists(result.source_database, config=config):
        raise RuntimeError("Temporary source database remains after cleanup.")
    if await database_exists(result.restore_database, config=config):
        raise RuntimeError("Temporary restore database remains after cleanup.")
    main_after = await database_snapshot(config.async_url)
    main_doctor_after = await run_database_doctor()
    if not main_doctor_after.ok or main_after != main_before:
        raise RuntimeError("Main database changed during the drill.")
    if postgres_connection(config).database != source_connection.database:
        raise RuntimeError("Configured main database changed during the drill.")
    return result


async def run() -> int:
    result = await run_drill()
    print(f"Temporary source database: {result.source_database}")
    print(f"Temporary restore database: {result.restore_database}")
    print(f"Memory rows: {result.source_snapshot.memories.count}")
    print(f"Vector rows: {result.source_snapshot.vectors.count}")
    print(f"Source Memory SHA-256: {result.source_snapshot.memories.sha256}")
    print(f"Restored Memory SHA-256: {result.restored_snapshot.memories.sha256}")
    print(f"Source vector SHA-256: {result.source_snapshot.vectors.sha256}")
    print(f"Restored vector SHA-256: {result.restored_snapshot.vectors.sha256}")
    print(f"Tombstone rows: {result.source_snapshot.tombstones.count}")
    print(f"Source tombstone SHA-256: {result.source_snapshot.tombstones.sha256}")
    print(f"Restored tombstone SHA-256: {result.restored_snapshot.tombstones.sha256}")
    print(f"Backup SHA-256: {result.backup_sha256}")
    print(f"Backup size: {result.backup_size} bytes")
    print(f"pg_dump: {result.pg_dump_version}")
    print("Restored doctor: OK")
    print("Temporary source cleanup: OK")
    print("Temporary restore cleanup: OK")
    print("Main database unchanged: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser().parse_args(argv)
    try:
        return asyncio.run(run())
    except Exception as exc:
        message = safe_error_message(exc, operation="Backup/restore drill")
        print(f"Backup/restore drill failed safely: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
