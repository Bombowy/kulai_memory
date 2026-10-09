from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_provider_ollama_embeddings import provider as provider_module
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.integration.test_memory_postgres import _require_opt_in
from kulai_memory.application import MEMORY_VECTOR_NAMESPACE, Memory
from kulai_memory.backfill import BackfillReader, EMBEDDING_MODEL
from kulai_memory.database_safety import (
    async_database_url, create_owned_temporary_database, database_config,
    drop_owned_temporary_database,
)
from kulai_memory.persistence import PostgresMemoryRepository
from scripts import index_memories as cli
from scripts.db_backup_restore_drill import migrate_owned_database


@asynccontextmanager
async def _fixture():
    config = database_config()
    owned = await create_owned_temporary_database(kind="backup", config=config)
    try:
        await migrate_owned_database(owned, config=config)
        engine = create_async_engine(async_database_url(database=owned.name, config=config))
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2025, 1, 1, tzinfo=UTC)
            memories = tuple(Memory(
                id=UUID(int=index), content=f"synthetic backfill memory {index}",
                created_at=base + timedelta(seconds=0 if index < 3 else 1),
                metadata={"private": "SYNTHETIC_PRIVATE_METADATA"},
            ) for index in (1, 2, 3))
            async with factory() as session:
                repo = PostgresMemoryRepository(db=session)
                for memory in reversed(memories):
                    await repo.create(memory)
                await session.commit()
            yield owned, config, engine, BackfillReader(factory), memories
        finally:
            await engine.dispose()
    finally:
        await drop_owned_temporary_database(owned, config=config)


def _args(output=None, *, reindex=False):
    values = [] if output is None else ["--execute", "--backup-output", str(output)]
    if reindex:
        values.append("--reindex")
    return cli.parser().parse_args(values)


async def _rows(reader):
    async with reader._read_session() as session:
        return (await session.execute(text("""
            SELECT pk, namespace_key, record_id, vector_dims(embedding) AS dimension, metadata_json
            FROM kulai_vector_records ORDER BY record_id
        """))).mappings().all()


def _verify_rows(rows, memories, *, provider_id):
    assert len(rows) == 3
    assert {row["record_id"] for row in rows} == {str(memory.id) for memory in memories}
    for row in rows:
        assert row["namespace_key"] == MEMORY_VECTOR_NAMESPACE
        assert row["dimension"] == 1024
        assert row["metadata_json"] == {
            "source_memory_id": row["record_id"], "embedding_provider_id": provider_id,
            "embedding_model_tag": EMBEDDING_MODEL, "embedding_dimension": 1024, "revision": 1,
        }


def _track_engines(monkeypatch, fixture_engine):
    engines = [fixture_engine]
    original = cli.create_async_engine
    def create(*args, **kwargs):
        engine = original(*args, **kwargs)
        engines.append(engine)
        return engine
    monkeypatch.setattr(cli, "create_async_engine", create)
    return engines


def _fake_factory(monkeypatch, engines, *, fail_second_memory=False):
    counts = {"providers": 0, "requests": 0, "closed": 0}

    class Provider:
        provider_id = "fake-embeddings"
        capabilities = EmbeddingCapabilities()
        def __init__(self):
            self.calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            counts["closed"] += 1
        async def embed(self, request):
            assert all(engine.pool.checkedout() == 0 for engine in engines)
            assert request.model_hint is None and request.purpose is None
            self.calls += 1
            counts["requests"] += 1
            if fail_second_memory and counts["providers"] == 1 and self.calls == 3:
                raise RuntimeError("PRIVATE_BACKFILL_PROVIDER_SENTINEL")
            return EmbeddingResponse(
                provider_id=self.provider_id, model_id=EMBEDDING_MODEL, dimension=1024,
                embeddings=(EmbeddingVector(values=(float(self.calls),) * 1024),),
            )

    def create(*, settings):
        counts["providers"] += 1
        return Provider()
    monkeypatch.setattr(cli, "create_embedding_provider", create)
    return counts


async def _fake_flow(monkeypatch, tmp_path):
    async with _fixture() as (owned, config, engine, reader, memories):
        counts = _fake_factory(monkeypatch, _track_engines(monkeypatch, engine))
        baseline = await reader.fingerprints()
        selection = await reader.select(limit=100, memory_id=None, reindex=False)
        assert selection.ids == tuple(memory.id for memory in memories)
        filtered = await reader.select(limit=1, memory_id=memories[1].id, reindex=False)
        assert filtered.ids == (memories[1].id,)
        assert await cli.run_owned(_args(), owned=owned, config=config) == 0
        assert counts["requests"] == 0
        assert (await reader.fingerprints())[1].count == 0
        first_archive = tmp_path / "first.dump"
        assert await cli.run_owned(_args(first_archive), owned=owned, config=config) == 0
        assert first_archive.is_file() and first_archive.stat().st_size > 0
        assert counts == {"providers": 1, "requests": 4, "closed": 1}
        rows = await _rows(reader)
        _verify_rows(rows, memories, provider_id="fake-embeddings")
        pks = {row["record_id"]: row["pk"] for row in rows}
        assert (await reader.fingerprints())[0] == baseline[0]
        assert (await reader.select(limit=100, memory_id=None, reindex=False)).ids == ()
        # A second execute still passes all backup/restore guards, with no Memory embedding.
        assert await cli.run_owned(_args(tmp_path / "empty.dump"), owned=owned, config=config) == 0
        assert counts == {"providers": 2, "requests": 5, "closed": 2}
        assert await cli.run_owned(_args(tmp_path / "reindex.dump", reindex=True), owned=owned, config=config) == 0
        rows = await _rows(reader)
        _verify_rows(rows, memories, provider_id="fake-embeddings")
        assert {row["record_id"]: row["pk"] for row in rows} == pks
        assert (await reader.fingerprints())[0] == baseline[0]
        # A different model remains an existing identity, flagged for explicit reindex.
        async with reader.factory() as session:
            await session.execute(text("""
                UPDATE kulai_vector_records SET metadata_json = jsonb_set(
                    metadata_json, '{embedding_model_tag}', '"different-model:tag"'::jsonb
                ) WHERE record_id = :id
            """), {"id": str(memories[0].id)})
            await session.commit()
        stale = await reader.select(limit=100, memory_id=None, reindex=False)
        assert stale.ids == () and stale.incompatible_existing == 3
        # Fake provider IDs differ from ollama, hence all three are reported incompatible.


async def _partial_resume(monkeypatch, tmp_path):
    async with _fixture() as (owned, config, engine, reader, memories):
        counts = _fake_factory(monkeypatch, _track_engines(monkeypatch, engine), fail_second_memory=True)
        baseline = await reader.fingerprints()
        assert await cli.run_owned(_args(tmp_path / "partial.dump"), owned=owned, config=config) == 1
        assert (await reader.fingerprints())[1].count == 1
        assert (await reader.fingerprints())[0] == baseline[0]
        assert await cli.run_owned(_args(tmp_path / "resume.dump"), owned=owned, config=config) == 0
        _verify_rows(await _rows(reader), memories, provider_id="fake-embeddings")
        assert (await reader.fingerprints())[0] == baseline[0]
        assert counts == {"providers": 2, "requests": 6, "closed": 2}


async def _real_flow(monkeypatch, tmp_path):
    async with _fixture() as (owned, config, engine, reader, memories):
        engines = _track_engines(monkeypatch, engine)
        counts = {"clients": 0, "requests": 0, "closed": 0, "overrides": 0}
        original = provider_module.AsyncClient
        def create(*args, **kwargs):
            counts["clients"] += 1
            client = original(*args, **kwargs)
            original_embed, original_close = client.embed, client.close
            async def embed(*args, **kwargs):
                assert all(engine.pool.checkedout() == 0 for engine in engines)
                counts["requests"] += 1
                if kwargs.get("dimensions") is not None:
                    counts["overrides"] += 1
                return await original_embed(*args, **kwargs)
            async def close():
                counts["closed"] += 1
                await original_close()
            monkeypatch.setattr(client, "embed", embed)
            monkeypatch.setattr(client, "close", close)
            return client
        monkeypatch.setattr(provider_module, "AsyncClient", create)
        before = await reader.fingerprints()
        assert await cli.run_owned(_args(tmp_path / "real_bge.dump"), owned=owned, config=config) == 0
        _verify_rows(await _rows(reader), memories, provider_id="ollama")
        after = await reader.fingerprints()
        assert after[0] == before[0] and after[1].count == 3
        assert counts == {"clients": 1, "requests": 4, "closed": 1, "overrides": 0}


def test_owned_backfill_fake_dry_execute_reindex_and_actual_backup_restore(monkeypatch, tmp_path):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_fake_flow(monkeypatch, tmp_path), timeout=180))


def test_owned_backfill_partial_failure_is_resumable(monkeypatch, tmp_path):
    _require_opt_in()
    asyncio.run(asyncio.wait_for(_partial_resume(monkeypatch, tmp_path), timeout=180))


def test_owned_backfill_real_bge_batch(monkeypatch, tmp_path):
    _require_opt_in()
    if os.environ.get("KULAI_RUN_OLLAMA_INTEGRATION") != "1":
        pytest.skip("Set KULAI_RUN_OLLAMA_INTEGRATION=1 for real BGE backfill.")
    asyncio.run(asyncio.wait_for(_real_flow(monkeypatch, tmp_path), timeout=180))
