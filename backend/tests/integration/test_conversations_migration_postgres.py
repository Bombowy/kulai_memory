"""0004 -> 0005 and existing pg_dump/restore, on marker-owned databases only."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import os
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.integration.test_memory_postgres import _upgrade_database
from backend.tests.integration.test_sessions_postgres import paired
from backend.tests.security_postgres_fixtures import owned_store
from kulai_memory.backup_service import create_verified_backup, revalidate_verified_backup
from kulai_memory.conversations import CompletedTurn
from kulai_memory.conversation_persistence import PostgresConversationRepository
from kulai_memory.conversation_persistence.integrity import TABLES
from kulai_memory.database_safety import (
    DatabaseSafetyError, async_database_url, create_owned_temporary_database,
    database_exists, database_snapshot_url, drop_owned_temporary_database,
    pre_migration_failures, require_owned_database, restore_archive_to_owned_database,
    run_database_doctor, validate_backup_doctor,
)
from kulai_memory.security_recovery.recovery import BackupEvidence, canonical_part, verify_archive
from kulai_memory.settings import get_settings
from scripts import db_backup, db_backup_restore_drill as drill, db_restore_smoke, migrate


def _downgrade_verified_owned(url):
    # Caller has verified the capability. Same environment handling as the
    # existing integration upgrade helper; never use a configured production URL.
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        config = migrate.alembic_config()
        assert migrate.configure_x_arguments(config)
        command.downgrade(config, "kulai_memory_0004")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def canonical_data(snapshot):
    return snapshot.memories, snapshot.vectors, snapshot.tombstones, snapshot.security


async def canonical_catalog(factory):
    async with factory() as db:
        return tuple(tuple(row) for row in await db.execute(text("""
            SELECT 'column',table_name,column_name,udt_name,is_nullable,
                   coalesce(column_default,''),coalesce(character_maximum_length::text,'')
            FROM information_schema.columns WHERE table_schema='public'
                AND table_name <> ALL(:excluded)
            UNION ALL SELECT 'constraint',r.relname,c.conname,c.contype::text,
                pg_get_constraintdef(c.oid),c.convalidated::text,c.condeferrable::text
            FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid
                JOIN pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname='public' AND r.relname <> ALL(:excluded)
            UNION ALL SELECT 'index',tablename,indexname,indexdef,'','',''
            FROM pg_indexes WHERE schemaname='public' AND tablename <> ALL(:excluded)
            ORDER BY 1,2,3
        """), {"excluded": [*TABLES, "alembic_version"]}))


def test_upgrade_from_four_preserves_canonical_security_and_legacy_backup(tmp_path):
    async def scenario():
        async with owned_store() as (owned, config, target, factory, _):
            await require_owned_database(owned, config=config)
            await asyncio.to_thread(_downgrade_verified_owned, target.async_url)
            await paired(factory)
            fixtures = await drill.seed_owned_source(owned, config=config, dimension=1024)
            assert fixtures.conversation is None
            before = await database_snapshot_url(target.async_url)
            catalog = await canonical_catalog(factory)
            assert before.revision == ("kulai_memory_0004",) and before.conversations is None
            assert before.memories.count == 2 and before.vectors.count == 1
            assert before.security.devices.count > 0
            report = await run_database_doctor(async_url=target.async_url, require_security=True)
            assert {c.name for c in report.checks if not c.ok} == pre_migration_failures("kulai_memory_0004", "kulai_memory_0005")
            with pytest.raises(DatabaseSafetyError):
                validate_backup_doctor(report)
            validate_backup_doctor(report, pre_migration_from="kulai_memory_0004")
            archive = tmp_path / "before-five.dump"
            await db_backup.create_owned_database_backup(archive, force=False, owned=owned, config=config,
                                                        pre_migration_from="kulai_memory_0004")
            restored = await db_restore_smoke.restore_owned_database_backup(archive, owned=owned, config=config,
                                                                           pre_migration_from="kulai_memory_0004")
            assert restored.snapshot == before and not await database_exists(restored.database, config=config)
            assert await database_snapshot_url(target.async_url) == before

            await require_owned_database(owned, config=config)
            await asyncio.to_thread(_upgrade_database, target.async_url, "kulai_memory_0005")
            assert (await run_database_doctor(async_url=target.async_url, require_security=True)).ok
            after = await database_snapshot_url(target.async_url)
            assert after.revision == ("kulai_memory_0005",)
            assert canonical_data(after) == canonical_data(before)
            assert await canonical_catalog(factory) == catalog
            assert [getattr(after.conversations, name).count for name in ("conversations", "turns", "messages")] == [0, 0, 0]
            repository = PostgresConversationRepository(factory)
            conversation = await repository.create(title="Po migracji / after migration")
            await repository.append_turn(conversation_id=conversation.conversation_id, completed=CompletedTurn(
                request_id=uuid4(), question="Gdzie jest klucz?", answer="In the drawer.", input_kind="voice",
                memory_ids=(fixtures.memories[0].id,)))
            assert len((await repository.recent_messages(conversation_id=conversation.conversation_id)).messages) == 2
            assert canonical_data(await database_snapshot_url(target.async_url)) == canonical_data(before)
            await drill.verify_fixtures(target.async_url, fixtures, dimension=1024)

            # Downgrade removes exactly the three new tables, then upgrade is repeatable.
            await require_owned_database(owned, config=config)
            await asyncio.to_thread(_downgrade_verified_owned, target.async_url)
            assert await database_snapshot_url(target.async_url) == before
            assert await canonical_catalog(factory) == catalog
            await asyncio.to_thread(_upgrade_database, target.async_url, "kulai_memory_0005")
            assert await database_snapshot_url(target.async_url) == after
    asyncio.run(asyncio.wait_for(scenario(), 180))


def test_verified_backup_restore_reopens_chat_and_preserves_request_identity(tmp_path):
    async def scenario():
        async with owned_store() as (owned, config, target, factory, _):
            await paired(factory)
            fixtures = await drill.seed_owned_source(owned, config=config, dimension=1024)
            conversation = fixtures.conversation
            assert conversation is not None
            repository = PostgresConversationRepository(factory)
            other = await repository.create(title="Independent empty conversation")
            source = await database_snapshot_url(target.async_url)
            assert [getattr(source.conversations, key).count for key in ("conversations", "turns", "messages")] == [2, 2, 4]
            backup = await create_verified_backup(tmp_path / "conversations.dump", config=config, app_env="test", owned=owned)
            assert backup.source_snapshot == source
            await revalidate_verified_backup(backup, config=target)
            # Security recovery archive comparisons include chat fingerprints too.
            evidence = BackupEvidence.from_backup(backup)
            assert canonical_part(await verify_archive(evidence, config=config)) == canonical_part(asdict(source))
            forged = asdict(source)
            forged["conversations"]["messages"]["sha256"] = "0" * 64
            from kulai_memory.security.models import SecurityError
            with pytest.raises(SecurityError):
                await verify_archive(replace(evidence, snapshot=forged), config=config)

            restored = await create_owned_temporary_database(kind="restore", config=config)
            restored_url = async_database_url(database=restored.name, config=config)
            try:
                await restore_archive_to_owned_database(backup.path, restored, config=config)
                assert await database_snapshot_url(restored_url) == source
                await drill.verify_fixtures(restored_url, fixtures, dimension=1024)
                # Fresh engine/connection, same model values, Unicode, modes and soft refs.
                engine = create_async_engine(restored_url, echo=False, hide_parameters=True)
                try:
                    reopened = PostgresConversationRepository(async_sessionmaker(engine, expire_on_commit=False))
                    page = await reopened.recent_messages(conversation_id=conversation.conversation_id)
                    assert page == fixtures.conversation_history
                    assert not (await reopened.recent_messages(conversation_id=other.conversation_id)).messages
                    user, answer = page.messages[:2]
                    retry = await reopened.append_turn(conversation_id=conversation.conversation_id,
                        completed=CompletedTurn(request_id=user.request_id, question=user.content, answer=answer.content,
                                                input_kind=user.input_kind, memory_ids=answer.memory_ids))
                    assert not retry.created and retry.turn.user.message_id == user.message_id
                    assert await database_snapshot_url(restored_url) == source
                finally:
                    await engine.dispose()
            finally:
                await drop_owned_temporary_database(restored, config=config)
            assert not await database_exists(restored.name, config=config)
            assert await database_snapshot_url(target.async_url) == source

            await repository.append_turn(conversation_id=conversation.conversation_id, completed=CompletedTurn(
                request_id=uuid4(), question="Another question", answer="Kolejna odpowiedź", input_kind="text"))
            changed = await database_snapshot_url(target.async_url)
            assert canonical_data(changed) == canonical_data(source)
            assert changed.conversations != source.conversations
            with pytest.raises(DatabaseSafetyError):
                await revalidate_verified_backup(backup, config=target)
    asyncio.run(asyncio.wait_for(scenario(), 240))


@pytest.mark.parametrize("sql,failed", [
    ("DROP TABLE public.kulai_conversation_messages", "conversation.tables"),
    ("ALTER TABLE public.kulai_conversations ADD COLUMN unexpected text", "conversation.schema"),
    ("ALTER TABLE public.kulai_conversation_messages DROP CONSTRAINT ck_conversation_message_content", "conversation.schema"),
    ("DROP INDEX public.ix_conversations_activity_id", "conversation.schema"),
    ("ALTER TABLE public.kulai_conversations ALTER COLUMN last_turn DROP DEFAULT", "conversation.schema"),
    ("UPDATE public.alembic_version SET version_num='kulai_memory_0004'", "alembic.current"),
    ("DELETE FROM public.kulai_conversation_messages WHERE role='assistant'", "conversation.integrity"),
])
def test_doctor_and_snapshot_reject_damaged_or_unversioned_chat(sql, failed):
    async def scenario():
        async with owned_store() as (owned, config, target, factory, _):
            await drill.seed_owned_source(owned, config=config, dimension=1024)
            before = await database_snapshot_url(target.async_url)
            assert (await run_database_doctor(async_url=target.async_url)).ok
            await require_owned_database(owned, config=config)
            async with factory.begin() as db:
                await db.execute(text(sql))
            report = await run_database_doctor(async_url=target.async_url)
            assert not next(c for c in report.checks if c.name == failed).ok
            with pytest.raises(DatabaseSafetyError):
                await database_snapshot_url(target.async_url)
            if failed == "alembic.current":
                # A complete but unversioned chat schema is not a reviewed legacy gap.
                with pytest.raises(DatabaseSafetyError):
                    validate_backup_doctor(report, pre_migration_from="kulai_memory_0004")
            # Synthetic canonical fixtures remain intact; no real database was a target.
            async with factory() as db:
                assert await db.scalar(text("SELECT count(*) FROM memories")) == before.memories.count
    asyncio.run(asyncio.wait_for(scenario(), 120))
