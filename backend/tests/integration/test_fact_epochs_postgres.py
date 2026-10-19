"""Durable generations, real trigger transactions and races on owned PostgreSQL."""

import asyncio
import json
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from backend.tests.fact_memory_fixtures import fact_store, staged_memory, create_memory, analyze_memory
from backend.tests.integration.test_fact_memory import approve, mutate_memory
from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.test_memory_intelligence import CASES, ConstantBGE, GoldenLLM
from kulai_memory.application.fact_memory import FactMemoryError, StageBatch, StageSource
from kulai_memory.application.intelligence import MemoryIntelligenceService, SourceDocument
from kulai_memory.database_safety import require_owned_database, database_snapshot_url, run_database_doctor
from kulai_memory.fact_persistence.migration import upgrade_owned_facts
from kulai_memory.fact_persistence.owned import open_owned_fact_memory
from kulai_memory.fact_persistence.repository import PostgresFactRepository, memory_source_id
from kulai_memory.fact_persistence.schema_v2 import TABLES


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 60))


async def epoch(state, memory_id):
    async with state.storage._tx(read_only=True) as repo:
        return dict(await repo.one("SELECT * FROM kulai_fact_memory_epochs WHERE memory_id=:id", {"id": memory_id}))


async def change(connection, memory_id, action):
    commands = {
        "edit": "UPDATE memories SET content='changed synthetic',revision=revision+1 WHERE id=:id",
        "archive": "UPDATE memories SET archived_at=clock_timestamp() WHERE id=:id",
        "restore": "UPDATE memories SET archived_at=NULL WHERE id=:id",
        "delete": "DELETE FROM memories WHERE id=:id",
    }
    await connection.execute(text(commands[action]), {"id": memory_id})


def test_epochs_increment_on_sql_changes_and_never_reopen_old_approval():
    async def scenario():
        async with fact_store() as state:
            memory, old, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            await approve(state, candidate_id)
            first = await epoch(state, memory.id)
            assert first["generation"] == 1
            await mutate_memory(state, memory.id, "archive")
            assert (await state.service.get_candidate(candidate_id)).status == "REVOKED"
            assert not await state.service.list_approved()
            await mutate_memory(state, memory.id, "restore")
            assert (await epoch(state, memory.id))["generation"] == 3
            assert not await state.service.list_approved()
            item = await analyze_memory(state, memory.id)
            assert item.source.revision == old.source.revision  # canonical semantics unchanged
            new = await state.service.stage(StageBatch(sources=(item,)))
            assert new.candidate_ids != staged.candidate_ids
            assert (await state.service.get_candidate(new.candidate_ids[0])).status == "PENDING"
            await approve(state, new.candidate_ids[0])
            await mutate_memory(state, memory.id, "edit", CASES["pl_switch"]["text"])
            assert (await epoch(state, memory.id))["generation"] == 4
            assert not await state.service.list_approved()
            edited = await analyze_memory(state, memory.id)
            assert edited.source.revision == 2
            latest = await state.service.stage(StageBatch(sources=(edited,)))
            assert (await state.service.get_candidate(latest.candidate_ids[0])).status == "PENDING"
            assert (await epoch(state, memory.id))["incarnation"] == first["incarnation"]
    run(scenario)


@pytest.mark.parametrize("action", ["edit", "archive", "archive_restore", "delete"])
def test_canonical_rollback_restores_epoch_facts_and_audit_atomically(action):
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            await approve(state, candidate_id)
            original = await epoch(state, memory.id)
            async with state.storage._engine.connect() as connection:
                transaction = await connection.begin()
                await change(connection, memory.id, "archive" if action == "archive_restore" else action)
                if action == "archive_restore":
                    await change(connection, memory.id, "restore")
                repo = PostgresFactRepository(connection)
                current = await repo.candidate_row(candidate_id)
                if action == "delete":
                    assert current is None
                else:
                    assert current["status"] == "REVOKED" and not current["source_valid"]
                # Other transactions still observe the old committed source AND
                # its valid approval, never a half-committed invalidation.
                assert len(await state.service.list_approved()) == 1
                await transaction.rollback()
            assert await epoch(state, memory.id) == original
            assert len(await state.service.list_approved()) == 1
            assert len(await state.service.list_decisions(candidate_id)) == 1
    run(scenario)


@pytest.mark.parametrize("action", ["edit", "archive", "delete"])
@pytest.mark.parametrize("first", ["mutation", "approval"])
def test_approve_races_canonical_mutation_in_both_lock_orders(action, first):
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            if first == "mutation":
                async with state.storage._engine.connect() as connection:
                    transaction = await connection.begin()
                    await change(connection, memory.id, action)
                    approval = asyncio.create_task(approve(state, candidate_id))
                    await asyncio.sleep(0.03)
                    assert not approval.done()
                    await transaction.commit()
                    result = (await asyncio.gather(approval, return_exceptions=True))[0]
                    assert isinstance(result, FactMemoryError)
            else:
                locked = asyncio.Event()
                def hold(connection, cursor, sql, parameters, context, executemany):
                    if sql.lstrip().startswith("INSERT INTO public.kulai_fact_approvals"):
                        locked.set()
                        connection.exec_driver_sql("SELECT pg_sleep(0.1)")
                event.listen(state.storage._engine.sync_engine, "before_cursor_execute", hold)
                async def mutate_after_lock():
                    await locked.wait()
                    async with state.storage._engine.begin() as connection:
                        await change(connection, memory.id, action)
                try:
                    receipt, _ = await asyncio.gather(approve(state, candidate_id), mutate_after_lock())
                    assert receipt.action == "approve"
                finally:
                    event.remove(state.storage._engine.sync_engine, "before_cursor_execute", hold)
            assert not await state.service.list_approved()
            record = await state.service.get_candidate(candidate_id)
            assert record is None if action == "delete" else not record.source_valid
    run(scenario)


def test_delete_purges_payloads_but_keeps_minimal_retirement_and_rejects_reinsert():
    async def scenario():
        async with fact_store() as state:
            memory, item, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            await approve(state, staged.candidate_ids[0])
            async with state.storage._engine.begin() as connection:
                await change(connection, memory.id, "delete")
            retired = await epoch(state, memory.id)
            assert retired["deleted_at"] and retired["generation"] == 2
            assert not await state.service.list_pending() and not await state.service.list_approved()
            with pytest.raises(FactMemoryError):
                await state.service.stage(StageBatch(sources=(item,)))
            async with state.storage._tx(read_only=True) as repo:
                all_rows = []
                for table in TABLES:
                    all_rows.extend(r["data"] for r in await repo.rows(f"SELECT row_to_json(t)::text AS data FROM public.{table} t"))
                assert "Flask" not in " ".join(all_rows)
            async with state.storage._engine.connect() as connection:
                with pytest.raises(Exception):
                    await connection.execute(text("""INSERT INTO memories(id,ingestion_id,content,source_kind,revision,created_at)
                        VALUES(:id,:ingest,:content,'voice',1,:created)"""),
                        {"id": memory.id, "ingest": uuid4(), "content": memory.content, "created": memory.created_at})
                await connection.rollback()
                with pytest.raises(Exception):
                    await connection.execute(text("TRUNCATE memories CASCADE"))
                await connection.rollback()
                with pytest.raises(Exception):
                    await connection.execute(text("DELETE FROM kulai_fact_memory_epochs WHERE memory_id=:id"), {"id": memory.id})
                await connection.rollback()
    run(scenario)


def test_noop_row_rewrite_vacuum_freeze_and_adapter_restart_preserve_approval():
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            await approve(state, staged.candidate_ids[0])
            original = await epoch(state, memory.id)
            async with state.storage._tx(read_only=True) as repo:
                assert (await repo.one("SELECT to_regclass('public.ix_fact_versions_live_source') IS NOT NULL AS present"))["present"]
            async with state.storage._engine.begin() as connection:
                old_xid = await connection.scalar(text("SELECT xmin::text FROM memories WHERE id=:id"), {"id": memory.id})
                await connection.execute(text("UPDATE memories SET content=content WHERE id=:id"), {"id": memory.id})
                new_xid = await connection.scalar(text("SELECT xmin::text FROM memories WHERE id=:id"), {"id": memory.id})
                assert new_xid != old_xid
            assert await epoch(state, memory.id) == original
            await require_owned_database(state.owned, config=state.config)
            async with state.storage._engine.connect() as raw:
                connection = await raw.execution_options(isolation_level="AUTOCOMMIT")
                actual, marker = (await connection.execute(text("SELECT current_database(),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))).one()
                assert actual == state.owned.name and marker == state.owned.marker
                await connection.execute(text("VACUUM (FREEZE, ANALYZE) public.memories, public.kulai_fact_memory_epochs"))
            async with open_owned_fact_memory(state.owned, config=state.config, intelligence=state.intelligence) as reopened:
                assert len(await reopened.service.list_approved()) == 1
                assert (await reopened.service.get_candidate(staged.candidate_ids[0])).status == "APPROVED"
            assert await epoch(state, memory.id) == original
            assert (await run_database_doctor(async_url=state.target.async_url)).ok
    run(scenario)


def test_cancellation_after_canonical_write_rolls_back_both_layers():
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            await approve(state, staged.candidate_ids[0])
            original = await epoch(state, memory.id)
            written = asyncio.Event()
            async def cancelled_edit():
                async with state.storage._engine.begin() as connection:
                    await change(connection, memory.id, "edit")
                    written.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(cancelled_edit())
            await written.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await epoch(state, memory.id) == original
            assert len(await state.service.list_approved()) == 1
            assert len(await state.service.list_decisions(staged.candidate_ids[0])) == 1
    run(scenario)


def test_deadlock_victim_rolls_back_every_canonical_and_fact_write():
    async def scenario():
        async with fact_store() as state:
            a, _, sa = await staged_memory(state, CASES["pl_flask"]["text"])
            b, _, sb = await staged_memory(state, CASES["pl_switch"]["text"])
            await approve(state, sa.candidate_ids[0]); await approve(state, sb.candidate_ids[0])
            ready_a, ready_b = asyncio.Event(), asyncio.Event()
            async def changes(first, second, mine, other):
                async with state.storage._engine.connect() as connection:
                    tx = await connection.begin()
                    try:
                        await change(connection, first, "edit")
                        mine.set(); await other.wait()
                        await change(connection, second, "edit")
                        await tx.commit()
                        return "committed"
                    except Exception as error:
                        await tx.rollback()
                        assert getattr(error.orig, "sqlstate", None) == "40P01"
                        return "deadlock"
            results = await asyncio.gather(changes(a.id, b.id, ready_a, ready_b), changes(b.id, a.id, ready_b, ready_a))
            assert sorted(results) == ["committed", "deadlock"]
            assert not await state.service.list_approved()
            for memory in (a, b):
                assert (await epoch(state, memory.id))["generation"] == 2
            assert len(await state.service.list_decisions(sa.candidate_ids[0])) == 2
            assert len(await state.service.list_decisions(sb.candidate_ids[0])) == 2
    run(scenario)


def test_v1_to_v2_migration_preserves_valid_decisions_and_invalidates_stale_legacy():
    async def scenario():
        from kulai_memory.application import Memory
        from kulai_memory.persistence import PostgresMemoryRepository
        async with owned_store(security=False) as (owned, config, target, factory, _):
            await upgrade_owned_facts(owned, config=config, revision="kulai_facts_0001")
            intelligence = MemoryIntelligenceService(llm_provider=GoldenLLM(), embedding_provider=ConstantBGE())
            async def legacy(content, action):
                # Explicit migration fixture encoded against unchanged v1 DDL.
                # Current approval tests use the real service; this represents
                # data committed by the previous schema/client before upgrade.
                async with factory() as session:
                    async with session.begin():
                        memory = await PostgresMemoryRepository(db=session).create(Memory(content=content))
                source = SourceDocument(source_id=f"memory-{memory.id}-r1", memory_id=memory.id,
                    revision=1, text=memory.content, created_at=memory.created_at)
                candidate = (await intelligence.extract(source)).candidates[0]
                source_id, version_id, fact_id, candidate_id, request_id = memory_source_id(memory.id), uuid4(), uuid4(), uuid4(), uuid4()
                async with factory() as session:
                    async with session.begin():
                        token = await session.scalar(text("SELECT xmin::text FROM memories WHERE id=:id"), {"id": memory.id})
                        await session.execute(text("INSERT INTO kulai_fact_sources(id,memory_id) VALUES(:s,:m)"), {"s": source_id, "m": memory.id})
                        await session.execute(text("""INSERT INTO kulai_fact_source_versions
                            (id,source_id,number,revision,sha256,source_token,document_meta,pipeline)
                            VALUES(:id,:s,1,1,:hash,:token,CAST(:meta AS jsonb),'synthetic-legacy-v1')"""),
                            {"id": version_id, "s": source_id, "hash": source.content_sha256, "token": token,
                             "meta": json.dumps(source.model_dump(mode="json", exclude={"text"}))})
                        await session.execute(text("INSERT INTO kulai_fact_identities(id,source_id) VALUES(:f,:s)"), {"f": fact_id, "s": source_id})
                        await session.execute(text("""INSERT INTO kulai_fact_candidates
                            (id,fact_id,source_version_id,version,ordinal,information_type,payload)
                            VALUES(:c,:f,:v,1,1,'fact',CAST(:payload AS jsonb))"""),
                            {"c": candidate_id, "f": fact_id, "v": version_id, "payload": candidate.model_dump_json()})
                        await session.execute(text("UPDATE kulai_fact_candidates SET status=:status,lock_version=2 WHERE id=:id"),
                            {"id": candidate_id, "status": "APPROVED" if action == "approve" else "REJECTED"})
                        if action == "approve":
                            await session.execute(text("INSERT INTO kulai_fact_approvals(candidate_id,approved_at,reviewer_id) VALUES(:c,clock_timestamp(),'local-test:legacy')"), {"c": candidate_id})
                        await session.execute(text("""INSERT INTO kulai_fact_decisions
                            (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
                            VALUES(:r,:c,'candidate',:action,'local-test:legacy',1,2)"""), {"r": request_id, "c": candidate_id, "action": action})
                return memory, source, candidate_id, request_id
            a = await legacy(CASES["pl_flask"]["text"], "approve")
            b = await legacy(CASES["pl_other"]["text"], "reject")
            c = await legacy(CASES["pl_switch"]["text"], "approve")
            deleted = await legacy(CASES["pl_flask"]["text"], "approve")
            from kulai_memory.deletion_persistence import delete_memory
            await delete_memory(memory_id=deleted[0].id, session_factory=factory)
            async with factory() as session:
                async with session.begin():
                    await session.execute(text("UPDATE memories SET archived_at=clock_timestamp() WHERE id=:id"), {"id": c[0].id})
                    await session.execute(text("UPDATE memories SET archived_at=NULL WHERE id=:id"), {"id": c[0].id})
            canonical_before = await database_snapshot_url(target.async_url)
            async with factory() as session:
                audit_before = (await session.execute(text("SELECT row_to_json(d)::text FROM kulai_fact_decisions d ORDER BY request_id"))).scalars().all()
            with pytest.raises(FactMemoryError, match="schema_required"):
                async with open_owned_fact_memory(owned, config=config, intelligence=intelligence):
                    pass  # Startup must NEVER install v2 implicitly.
            await upgrade_owned_facts(owned, config=config)
            assert await database_snapshot_url(target.async_url) == canonical_before
            async with open_owned_fact_memory(owned, config=config, intelligence=intelligence) as local:
                assert [r.id for r in await local.service.list_approved()] == [a[2]]
                assert (await local.service.get_candidate(b[2])).status == "REJECTED"
                assert (await local.service.get_candidate(c[2])).status == "REVOKED"
                assert await local.service.get_candidate(deleted[2]) is None
                for item in (a, b, c, deleted):
                    assert (await local.storage.receipt(item[3])).request_id == item[3]
                source = await local.storage.memory_document(a[0].id)
                extraction = await intelligence.extract(source)
                retry = await local.service.stage(StageBatch(sources=(StageSource(source=source, extraction=extraction),)))
                assert retry.candidate_ids == (a[2],) and retry.reused_sources == 1
                async with local.storage._tx(read_only=True) as repo:
                    retired = await repo.one("SELECT deleted_at FROM kulai_fact_memory_epochs WHERE memory_id=:id", {"id": deleted[0].id})
                    assert retired["deleted_at"] is not None
                    audit_after = [r["data"] for r in await repo.rows("SELECT row_to_json(d)::text AS data FROM kulai_fact_decisions d ORDER BY request_id")]
                    assert set(audit_before) <= set(audit_after)
            assert (await run_database_doctor(async_url=target.async_url)).ok
    run(scenario)


def test_sql_content_aba_without_revision_change_never_restores_approval():
    async def scenario():
        async with fact_store() as state:
            memory, original_item, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            await approve(state, staged.candidate_ids[0])
            for content in ("different synthetic content", memory.content):
                async with state.storage._engine.begin() as connection:
                    await connection.execute(text("UPDATE memories SET content=:content WHERE id=:id"), {"id": memory.id, "content": content})
            current = await state.storage.memory_document(memory.id)
            assert current.revision == original_item.source.revision == 1
            assert current.content_sha256 == original_item.source.content_sha256
            assert (await epoch(state, memory.id))["generation"] == 3
            assert not await state.service.list_approved()
            assert (await state.service.get_candidate(staged.candidate_ids[0])).status == "REVOKED"
            item = await analyze_memory(state, memory.id)
            new = await state.service.stage(StageBatch(sources=(item,)))
            assert new.candidate_ids != staged.candidate_ids
            assert (await state.service.get_candidate(new.candidate_ids[0])).status == "PENDING"
    run(scenario)


def test_unstaged_extraction_cannot_cross_archive_restore_generation():
    async def scenario():
        async with fact_store() as state:
            memory = await create_memory(state, CASES["pl_flask"]["text"])
            old = await analyze_memory(state, memory.id)
            await mutate_memory(state, memory.id, "archive")
            await mutate_memory(state, memory.id, "restore")
            assert (await epoch(state, memory.id))["generation"] == 3
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await state.service.stage(StageBatch(sources=(old,)))
            assert not await state.service.list_pending()
            fresh = await analyze_memory(state, memory.id)
            assert fresh.source.revision == old.source.revision
            assert fresh.source.content_sha256 == old.source.content_sha256
            assert fresh.source.source_id != old.source.source_id
            pending = await state.service.stage(StageBatch(sources=(fresh,)))
            await approve(state, pending.candidate_ids[0])
    run(scenario)


def test_cold_python_process_reads_durable_approval_then_observes_revocation():
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            await approve(state, staged.candidate_ids[0])
            code = """
import asyncio,json,sys
from kulai_memory.database_safety import OwnedTemporaryDatabase,database_config
from kulai_memory.application.intelligence import MemoryIntelligenceService
from kulai_memory.fact_persistence.owned import open_owned_fact_memory
async def main():
    value=json.load(sys.stdin)
    owned=OwnedTemporaryDatabase(value['name'],value['marker'],'backup')
    intelligence=MemoryIntelligenceService(llm_provider=None,embedding_provider=None)
    async with open_owned_fact_memory(owned,config=database_config(),intelligence=intelligence) as local:
        rows=await local.service.list_approved()
        print(json.dumps({'approved':len(rows)}))
try: asyncio.run(main())
except Exception:
    print(json.dumps({'error':'cold_reader_failed'}))
    sys.exit(1)
"""
            capability = json.dumps(dict(name=state.owned.name, marker=state.owned.marker))
            async def cold_read():
                result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code],
                    input=capability, capture_output=True, text=True, timeout=15)
                assert result.returncode == 0, "Cold owned reader failed"
                return json.loads(result.stdout)["approved"]
            assert await cold_read() == 1
            await mutate_memory(state, memory.id, "archive")
            await mutate_memory(state, memory.id, "restore")
            assert await cold_read() == 0
            assert (await state.service.get_candidate(staged.candidate_ids[0])).status == "REVOKED"
    run(scenario)


def test_approved_relation_is_revoked_in_canonical_transaction():
    async def scenario():
        async with fact_store() as state:
            a = await create_memory(state, CASES["pl_flask"]["text"])
            b = await create_memory(state, CASES["pl_switch"]["text"])
            docs = tuple([await state.storage.memory_document(m.id) for m in (a, b)])
            state.llm.relation = "possible_supersession"
            preview = await state.intelligence.preview(docs[0], (docs[1],))
            staged = await state.service.stage(StageBatch(sources=tuple(StageSource(source=s, extraction=e)
                for s, e in zip(docs, preview.extractions)), relations=preview.relations))
            for candidate_id in staged.candidate_ids:
                await approve(state, candidate_id)
            relation_id = staged.relation_ids[0]
            await state.service.approve_relation(relation_id, principal=state.principal, request_id=uuid4(), expected_version=1)
            async with state.storage._engine.connect() as connection:
                tx = await connection.begin()
                await change(connection, a.id, "archive")
                assert await connection.scalar(text("SELECT status FROM kulai_fact_relations WHERE id=:id"), {"id": relation_id}) == "REVOKED"
                await tx.rollback()
            assert len(await state.service.list_relations(status="APPROVED")) == 1
            await mutate_memory(state, a.id, "archive")
            assert not await state.service.list_relations(status="APPROVED")
            history = await state.service.list_decisions(relation_id)
            assert len(history) == 2 and history[-1].action == "invalidate"
    run(scenario)


def test_cancellation_after_approval_writes_before_commit_rolls_back():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            written = asyncio.Event()
            def pause(connection, cursor, sql, parameters, context, executemany):
                if sql.lstrip().startswith("INSERT INTO public.kulai_fact_approvals"):
                    written.set()
                    connection.exec_driver_sql("SELECT pg_sleep(0.2)")
            engine = state.storage._engine.sync_engine
            event.listen(engine, "before_cursor_execute", pause)
            try:
                task = asyncio.create_task(approve(state, staged.candidate_ids[0]))
                await written.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                event.remove(engine, "before_cursor_execute", pause)
            assert (await state.service.get_candidate(staged.candidate_ids[0])).status == "PENDING"
            assert not await state.service.list_decisions(staged.candidate_ids[0])
            assert state.storage._engine.pool.checkedout() == 0
    run(scenario)


def test_read_latency_and_bounded_epoch_join(capsys):
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            await approve(state, staged.candidate_ids[0])
            times = []
            for _ in range(50):
                start = perf_counter()
                assert len(await state.service.list_approved(limit=50)) == 1
                times.append((perf_counter() - start) * 1000)
            times.sort()
            print("FACT_READ_MS=" + json.dumps(dict(n=50, median=round(times[25], 3), p95=round(times[47], 3), maximum=round(times[-1], 3))))
    run(scenario)
    # Only synthetic timing metadata is emitted, no claims/quotes.
    output = capsys.readouterr().out
    assert "FACT_READ_MS=" in output
    with capsys.disabled():
        print(output.strip())
