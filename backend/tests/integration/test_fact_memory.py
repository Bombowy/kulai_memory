"""Real PostgreSQL fact lifecycle, privacy, transactions and concurrency."""

import asyncio
import copy
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from backend.tests.fact_memory_fixtures import analyze_memory, create_memory, fact_store, staged_memory
from backend.tests.test_memory_intelligence import CASES, PRIVATE
from kulai_memory.application import MemoryLifecycleService
from kulai_memory.application.fact_memory import FactMemoryError, RevisionBinding, StageBatch, StageSource
from kulai_memory.application.intelligence.models import VerificationBatch, VerificationVerdict
from kulai_memory.database_safety import database_exists, database_snapshot_url, run_database_doctor
from kulai_memory.deletion_persistence import delete_memory
from kulai_memory.fact_persistence.migration import fact_migration_heads, upgrade_owned_facts
from kulai_memory.fact_persistence.owned import open_owned_fact_memory
from kulai_memory.fact_persistence.repository import memory_source_id
from kulai_memory.fact_persistence.schema_v2 import TABLES
from kulai_memory.persistence import PostgresMemoryRepository


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 120))


async def approve(state, candidate_id, *, request_id=None, expected_version=1):
    return await state.service.approve(candidate_id, principal=state.principal,
        request_id=request_id or uuid4(), expected_version=expected_version)


async def mutate_memory(state, memory_id, action, content=None):
    from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig
    async with state.factory() as session:
        async with session.begin():
            lifecycle = MemoryLifecycleService(repository=PostgresMemoryRepository(db=session),
                store=PgVectorStore(db=session, config=PgVectorStoreConfig(dimension=1024)))
            if action == "edit":
                return await lifecycle.edit(memory_id=memory_id, content=content, expected_revision=1)
            if action == "archive": return await lifecycle.archive(memory_id=memory_id)
            return await lifecycle.restore(memory_id=memory_id)


def test_independent_schema_and_pending_approved_with_unchanged_canonical():
    async def scenario():
        async with fact_store(security=True) as state:
            assert fact_migration_heads() == ("kulai_facts_0002",)
            memory = await create_memory(state, CASES["pl_flask"]["text"])
            before = await database_snapshot_url(state.target.async_url)
            item = await analyze_memory(state, memory.id)
            staged = await state.service.stage(StageBatch(sources=(item,)))
            candidate_id = staged.candidate_ids[0]
            assert candidate_id != memory.id
            assert (await state.service.get_candidate(candidate_id)).status == "PENDING"
            assert not await state.service.list_approved()
            assert len(await state.service.list_pending()) == 1
            receipt = await approve(state, candidate_id)
            assert receipt.reviewer_id.startswith("local-test:") and receipt.decided_at.utcoffset() is not None
            record = (await state.service.list_approved())[0]
            assert record.status == "APPROVED" and record.lock_version == 2
            assert record.candidate.provenance.memory_id == memory.id
            assert record.candidate.provenance.revision == 1
            assert await database_snapshot_url(state.target.async_url) == before
            assert (await run_database_doctor(async_url=state.target.async_url, require_security=True)).ok
            print("facts.lifecycle=PENDING->APPROVED;canonical_and_security_unchanged=true")
        assert not await database_exists(state.owned.name, config=state.config)
    run(scenario)


@pytest.mark.parametrize("action", ["reject", "revoke"])
def test_rejected_revoked_history_and_no_automatic_reopening(action):
    async def scenario():
        async with fact_store() as state:
            _, item, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            version = 1
            if action == "revoke":
                await approve(state, candidate_id); version = 2
            method = state.service.reject if action == "reject" else state.service.revoke_approval
            await method(candidate_id, principal=state.principal, request_id=uuid4(), expected_version=version)
            result = await state.service.stage(StageBatch(sources=(item,)))
            record = await state.service.get_candidate(candidate_id)
            assert result.candidate_ids == staged.candidate_ids and result.reused_sources == 1
            assert record.status == ("REJECTED" if action == "reject" else "REVOKED")
            assert not await state.service.list_approved() and not await state.service.list_pending()
            with pytest.raises(FactMemoryError, match="invalid_transition"):
                await approve(state, candidate_id, expected_version=record.lock_version)
            assert len(await state.service.list_decisions(candidate_id)) == (1 if action == "reject" else 2)
    run(scenario)


def test_first_analysis_wins_when_qwen_returns_different_claims_and_order():
    async def scenario():
        async with fact_store() as state:
            _, item, staged = await staged_memory(state, CASES["pl_multi"]["text"])
            await approve(state, staged.candidate_ids[0])
            altered = item.extraction.model_copy(update={"candidates": tuple(reversed(item.extraction.candidates[:2]))})
            result = await state.service.stage(StageBatch(sources=(item.model_copy(update={"extraction": altered}),)))
            assert result.candidate_ids == staged.candidate_ids and result.reused_sources == 1
            assert len(await state.service.list_pending()) == 3
            assert len(await state.service.list_approved()) == 1
            record = await state.service.get_candidate(staged.candidate_ids[0])
            assert record.candidate.model_dump(mode="json") == item.extraction.candidates[0].model_dump(mode="json")
    run(scenario)


def test_decision_idempotence_and_conflicting_reuse():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id, request = staged.candidate_ids[0], uuid4()
            first = await approve(state, candidate_id, request_id=request)
            second = await approve(state, candidate_id, request_id=request)
            assert first == second and len(await state.service.list_decisions(candidate_id)) == 1
            with pytest.raises(FactMemoryError, match="idempotency_conflict"):
                await state.service.reject(candidate_id, principal=state.principal, request_id=request, expected_version=1)
    run(scenario)


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_decisions_have_one_winner(same_request):
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id, request = staged.candidate_ids[0], uuid4()
            async def decision(action, key):
                try:
                    return await getattr(state.service, action)(candidate_id, principal=state.principal,
                        request_id=key, expected_version=1)
                except FactMemoryError as exc: return exc
            results = await asyncio.gather(decision("approve", request),
                decision("approve" if same_request else "reject", request if same_request else uuid4()))
            if same_request:
                assert results[0] == results[1]
            else:
                assert sum(isinstance(r, FactMemoryError) for r in results) == 1
            assert len(await state.service.list_decisions(candidate_id)) == 1
            assert (await state.service.get_candidate(candidate_id)).lock_version == 2
    run(scenario)


@pytest.mark.parametrize("phase", ["pending", "approved"])
def test_edit_invalidates_source_and_explicit_revision_preserves_identity(phase):
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            old = await state.service.get_candidate(candidate_id)
            if phase == "approved": await approve(state, candidate_id)
            await mutate_memory(state, memory.id, "edit", CASES["pl_switch"]["text"])
            assert not await state.service.list_approved()
            stale = await state.service.get_candidate(candidate_id)
            assert not stale.source_valid and stale.candidate is None
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await approve(state, candidate_id, expected_version=stale.lock_version)
            item = await analyze_memory(state, memory.id)
            binding = RevisionBinding(preview_candidate_id=item.extraction.candidates[0].candidate_id, fact_id=old.fact_id)
            second = await state.service.stage(StageBatch(sources=(item.model_copy(update={"revisions": (binding,)}),)))
            newer = await state.service.get_candidate(second.candidate_ids[0])
            assert newer.fact_id == old.fact_id and newer.version == 2 and newer.status == "PENDING"
            assert newer.source_version_id != old.source_version_id
            if phase == "approved": assert (await state.service.get_candidate(candidate_id)).status == "REVOKED"
            assert await state.service.list_decisions(old.source_version_id)
    run(scenario)


@pytest.mark.parametrize("reconcile", [False, True])
def test_archive_restore_cannot_silently_restore_approved_facts(reconcile):
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            await approve(state, candidate_id)
            await mutate_memory(state, memory.id, "archive")
            assert not await state.service.list_approved()
            if reconcile: await state.storage.reconcile_memory(memory.id)
            await mutate_memory(state, memory.id, "restore")
            assert not await state.service.list_approved()
            stale = await state.service.get_candidate(candidate_id)
            assert not stale.source_valid and stale.candidate is None
            item = await analyze_memory(state, memory.id)
            fresh = await state.service.stage(StageBatch(sources=(item,)))
            assert fresh.candidate_ids != staged.candidate_ids
            assert (await state.service.get_candidate(fresh.candidate_ids[0])).status == "PENDING"
    run(scenario)


@pytest.mark.parametrize("approved", [False, True])
def test_delete_cascades_all_derived_content_and_keeps_only_minimal_audit(approved):
    async def scenario():
        async with fact_store() as state:
            memory, item, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id, request = staged.candidate_ids[0], uuid4()
            if approved: await approve(state, candidate_id, request_id=request)
            await delete_memory(memory_id=memory.id, session_factory=state.factory, expected_revision=1)
            assert await state.service.get_candidate(candidate_id) is None
            assert not await state.service.list_approved() and not await state.service.list_pending()
            with pytest.raises(FactMemoryError, match="not_found"):
                await approve(state, candidate_id)
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await state.service.stage(StageBatch(sources=(item,)))
            async with state.factory() as session:
                for table in TABLES:
                    rows = (await session.execute(text(f"SELECT row_to_json(t)::text FROM public.{table} t"))).scalars().all()
                    assert "Flask" not in " ".join(rows) and memory.content not in " ".join(rows)
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_sources")) == 0
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_candidates")) == 0
            decisions = await state.service.list_decisions(candidate_id)
            assert any(d.action == "source_deleted" for d in decisions)
            if approved:
                # Historical idempotent receipt never recreates the deleted candidate.
                await approve(state, candidate_id, request_id=request)
                assert await state.service.get_candidate(candidate_id) is None
    run(scenario)


@pytest.mark.parametrize("tamper", ["hash", "revision", "recorded_at", "offset", "quote", "scope"])
def test_provenance_tampering_is_rejected_before_writes(tamper):
    async def scenario():
        async with fact_store() as state:
            memory = await create_memory(state, CASES["pl_flask"]["text"])
            item = await analyze_memory(state, memory.id)
            candidate = item.extraction.candidates[0]
            p = candidate.provenance
            if tamper == "hash": p = p.model_copy(update={"source_sha256": "0" * 64})
            if tamper == "revision": p = p.model_copy(update={"revision": 9})
            if tamper == "recorded_at": p = p.model_copy(update={"recorded_at": None})
            if tamper == "offset": p = p.model_copy(update={"evidence": (p.evidence[0].model_copy(update={"start": 1}),)})
            if tamper == "quote": p = p.model_copy(update={"evidence": (p.evidence[0].model_copy(update={"quote": PRIVATE}),)})
            candidate = candidate.model_copy(update={"provenance": p, **({"subject_scope": PRIVATE} if tamper == "scope" else {})})
            bad = item.model_copy(update={"extraction": item.extraction.model_copy(update={"candidates": (candidate,)})})
            with pytest.raises(FactMemoryError, match="invalid_provenance"):
                await state.service.stage(StageBatch(sources=(bad,)))
            assert not await state.service.list_pending()
    run(scenario)


def test_request_strings_and_foreign_capabilities_never_authorize_review():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            for principal in ("admin", {"reviewer": "admin"}, object()):
                with pytest.raises(FactMemoryError, match="forbidden"):
                    await state.service.approve(staged.candidate_ids[0], principal=principal, request_id=uuid4(), expected_version=1)
            async with open_owned_fact_memory(state.owned, config=state.config, intelligence=state.intelligence) as other:
                with pytest.raises(FactMemoryError, match="forbidden"):
                    await state.service.reject(staged.candidate_ids[0], principal=other.principal, request_id=uuid4(), expected_version=1)
            assert (await state.service.get_candidate(staged.candidate_ids[0])).status == "PENDING"
    run(scenario)


@pytest.mark.parametrize("failure", ["unsupported", "error", "cancel"])
def test_qwen_verification_never_commits_approval_on_failure(failure, caplog):
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            async def verify(*args):
                if failure == "error": raise RuntimeError(PRIVATE)
                if failure == "cancel": raise asyncio.CancelledError()
                return VerificationBatch(verdicts=[VerificationVerdict(
                    candidate_id="irrelevant", verdict="unsupported", reason="not_entailed")])
            state.intelligence.verify_candidates = verify
            exception = asyncio.CancelledError if failure == "cancel" else RuntimeError
            with pytest.raises(exception):
                await approve(state, candidate_id)
            assert (await state.service.get_candidate(candidate_id)).status == "PENDING"
            assert not await state.service.list_decisions(candidate_id)
    run(scenario)
    assert PRIVATE not in caplog.text


def test_source_edit_between_semantic_check_and_commit_is_detected():
    async def scenario():
        async with fact_store() as state:
            memory, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            original = state.intelligence.verify_candidates
            async def edit_after_check(*args):
                result = await original(*args)
                assert state.storage._engine.pool.checkedout() == 0
                await mutate_memory(state, memory.id, "edit", CASES["pl_switch"]["text"])
                return result
            state.intelligence.verify_candidates = edit_after_check
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await approve(state, staged.candidate_ids[0])
            assert not await state.service.list_decisions(staged.candidate_ids[0])
    run(scenario)


def test_commit_failure_rolls_back_candidate_and_decision(caplog):
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            wrote = False
            def statement(connection, cursor, sql, parameters, context, executemany):
                nonlocal wrote
                if sql.lstrip().upper().startswith("INSERT INTO PUBLIC.KULAI_FACT_DECISIONS"):
                    wrote = True
            def fail(connection):
                if wrote: raise RuntimeError(PRIVATE)
            sync = state.storage._engine.sync_engine
            event.listen(sync, "before_cursor_execute", statement)
            event.listen(sync, "commit", fail)
            try:
                with pytest.raises(FactMemoryError, match="operation_failed"):
                    await approve(state, candidate_id)
            finally:
                event.remove(sync, "before_cursor_execute", statement); event.remove(sync, "commit", fail)
            assert (await state.service.get_candidate(candidate_id)).status == "PENDING"
            assert not await state.service.list_decisions(candidate_id)
            assert state.storage._engine.pool.checkedout() == 0
    run(scenario)
    assert PRIVATE not in caplog.text


def test_lost_commit_ack_can_be_retried_without_second_decision():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id, request = staged.candidate_ids[0], uuid4()
            original = state.storage.decide
            async def lost_ack(**kwargs):
                await original(**kwargs)
                raise FactMemoryError()
            state.storage.decide = lost_ack
            recovered = await approve(state, candidate_id, request_id=request)
            state.storage.decide = original
            receipt = await approve(state, candidate_id, request_id=request)
            assert receipt == recovered and receipt.request_id == request
            assert len(await state.service.list_decisions(candidate_id)) == 1
            assert len(await state.service.list_approved()) == 1
    run(scenario)


def test_batch_staging_is_atomic_on_stale_second_source():
    async def scenario():
        async with fact_store() as state:
            a = await create_memory(state, CASES["pl_flask"]["text"])
            b = await create_memory(state, CASES["pl_other"]["text"])
            first, second = await analyze_memory(state, a.id), await analyze_memory(state, b.id)
            await mutate_memory(state, b.id, "edit", CASES["pl_switch"]["text"])
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await state.service.stage(StageBatch(sources=(first, second)))
            async with state.factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_source_versions")) == 0
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_candidates")) == 0
    run(scenario)


def test_fact_approval_does_not_approve_conflict_or_delete_older_fact():
    async def scenario():
        async with fact_store() as state:
            a = await create_memory(state, CASES["pl_flask"]["text"])
            b = await create_memory(state, CASES["pl_switch"]["text"])
            source_a, source_b = await state.storage.memory_document(a.id), await state.storage.memory_document(b.id)
            state.llm.relation = "possible_supersession"
            preview = await state.intelligence.preview(source_a, (source_b,))
            staged = await state.service.stage(StageBatch(sources=(
                StageSource(source=source_a, extraction=preview.extractions[0]),
                StageSource(source=source_b, extraction=preview.extractions[1]),
            ), relations=preview.relations))
            relation_id = staged.relation_ids[0]
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await state.service.approve_relation(relation_id, principal=state.principal, request_id=uuid4(), expected_version=1)
            for candidate_id in staged.candidate_ids: await approve(state, candidate_id)
            assert not await state.service.list_relations(status="APPROVED")
            assert len(await state.service.list_approved()) == 2
            await state.service.approve_relation(relation_id, principal=state.principal, request_id=uuid4(), expected_version=1)
            assert len(await state.service.list_relations(status="APPROVED")) == 1
            assert len(await state.service.list_approved()) == 2
            await delete_memory(memory_id=a.id, session_factory=state.factory)
            assert not await state.service.list_relations(status="APPROVED")
            assert len(await state.service.list_approved()) == 1
            async with state.factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_relations")) == 0
                audit = (await session.execute(text("SELECT row_to_json(d)::text FROM public.kulai_fact_decisions d"))).scalars().all()
                assert "Flask" not in " ".join(audit) and "FastAPI" not in " ".join(audit)
    run(scenario)


def test_local_sources_have_explicit_registration_and_privacy_retirement():
    async def scenario():
        async with fact_store() as state:
            key = uuid4()
            source = await state.storage.register_local(key, CASES["en_preference"]["text"])
            extraction = await state.intelligence.extract(source)
            item = StageSource(source=source, extraction=extraction)
            staged = await state.service.stage(StageBatch(sources=(item,)))
            await approve(state, staged.candidate_ids[0])
            assert (await state.service.list_approved())[0].candidate.fact.information_type == "preference"
            await state.storage.forget_local(key)
            assert not await state.service.list_approved()
            with pytest.raises(FactMemoryError, match="source_invalid"):
                await state.storage.register_local(key, source.text)
    run(scenario)


def test_changed_marker_is_checked_on_each_transaction_and_migration():
    async def scenario():
        async with fact_store() as state:
            from dataclasses import replace
            from kulai_memory.database_safety import set_owned_database_marker
            forged = replace(state.owned, marker="kulai-memory-backup-test:" + "0" * 32)
            await set_owned_database_marker(forged, config=state.config)
            try:
                with pytest.raises(FactMemoryError, match="owned_database_required"):
                    await state.service.list_pending()
                with pytest.raises(FactMemoryError, match="owned_database_required"):
                    await upgrade_owned_facts(state.owned, config=state.config)
            finally:
                await set_owned_database_marker(state.owned, config=state.config)
    run(scenario)


def test_db_content_is_immutable_and_queries_are_bounded():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id = staged.candidate_ids[0]
            await approve(state, candidate_id)
            async with state.factory() as session:
                with pytest.raises(Exception):
                    await session.execute(text("UPDATE public.kulai_fact_candidates SET payload='{}'::jsonb WHERE id=:id"), {"id": candidate_id})
                await session.rollback()
                indexes = (await session.execute(text("SELECT indexname FROM pg_indexes WHERE tablename LIKE 'kulai_fact_%'"))).scalars().all()
                assert "ix_fact_candidates_status_id" in indexes and "ix_fact_decisions_target" in indexes
            for limit in (0, 101, True):
                with pytest.raises(FactMemoryError, match="invalid_input"):
                    await state.service.list_approved(limit=limit)
            assert len(await state.service.list_approved(limit=1)) == 1
            assert not await state.service.list_approved(limit=1, after_id=candidate_id)
    run(scenario)


def test_idempotent_approval_rechecks_receipt_after_concurrent_preflight():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_flask"]["text"])
            candidate_id, request = staged.candidate_ids[0], uuid4()
            first = await approve(state, candidate_id, request_id=request)
            original = state.storage.receipt
            calls = 0
            async def initial_read_before_commit(key):
                nonlocal calls
                calls += 1
                return None if calls == 1 else await original(key)
            state.storage.receipt = initial_read_before_commit
            assert await approve(state, candidate_id, request_id=request) == first
            assert len(await state.service.list_decisions(candidate_id)) == 1
    run(scenario)


@pytest.mark.parametrize("failure", [None, "error", "cancel"])
def test_existing_runtime_stage_boundary_and_extraction_failure_no_writes(monkeypatch, failure):
    async def scenario():
        async with fact_store() as state:
            from kulai_memory import memory_intelligence_runtime as runtime_module
            from kulai_memory.settings import Settings
            monkeypatch.setattr(runtime_module, "create_llm_provider", lambda **kw: state.llm)
            monkeypatch.setattr(runtime_module, "create_embedding_provider", lambda **kw: state.bge)
            memory = await create_memory(state, CASES["pl_flask"]["text"])
            if failure:
                async def broken(request):
                    assert state.storage._engine.pool.checkedout() == 0
                    if failure == "cancel": raise asyncio.CancelledError()
                    raise RuntimeError(PRIVATE)
                state.llm.generate = broken
            runtime = runtime_module.MemoryIntelligenceRuntime(settings=Settings(_env_file=None))
            from kulai_memory.application.intelligence import IntelligenceError
            with pytest.raises(IntelligenceError):
                _ = runtime.intelligence
            async with runtime:
                async with open_owned_fact_memory(state.owned, config=state.config, intelligence=runtime.intelligence) as local:
                    if failure:
                        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
                        with pytest.raises(expected):
                            await local.analyze_and_stage(runtime, (memory.id,))
                    else:
                        preview, staged = await local.analyze_and_stage(runtime, (memory.id,))
                        assert preview.dry_run and staged.candidate_ids
                        assert (await local.service.list_pending())[0].status == "PENDING"
                        await local.service.approve(staged.candidate_ids[0], principal=local.principal,
                            request_id=uuid4(), expected_version=1)
                        assert len(await local.service.list_approved()) == 1
            with pytest.raises(IntelligenceError):
                _ = runtime.intelligence
            assert state.llm.closed == state.bge.closed == 1
            if failure:
                assert not await state.service.list_pending()
                async with state.factory() as session:
                    assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_sources")) == 0
    run(scenario)


def test_approved_versions_survive_adapter_reopening_and_preserve_information_type():
    async def scenario():
        async with fact_store() as state:
            _, _, staged = await staged_memory(state, CASES["pl_plan"]["text"])
            candidate_id = staged.candidate_ids[0]
            await approve(state, candidate_id)
            async with open_owned_fact_memory(state.owned, config=state.config, intelligence=state.intelligence) as reopened:
                records = await reopened.service.list_approved()
                assert len(records) == 1 and records[0].id == candidate_id
                assert records[0].candidate.fact.information_type == "plan"
                assert len(await reopened.service.list_decisions(candidate_id)) == 1
    run(scenario)


def test_concurrent_stage_reuses_one_complete_source_analysis():
    async def scenario():
        async with fact_store() as state:
            memory = await create_memory(state, CASES["pl_multi"]["text"])
            item = await analyze_memory(state, memory.id)
            batch = StageBatch(sources=(item,))
            first, second = await asyncio.gather(state.service.stage(batch), state.service.stage(batch))
            assert first.candidate_ids == second.candidate_ids
            assert sorted((first.reused_sources, second.reused_sources)) == [0, 1]
            assert len(await state.service.list_pending()) == 4
            async with state.factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_source_versions")) == 1
                assert await session.scalar(text("SELECT count(*) FROM public.kulai_fact_identities")) == 4
    run(scenario)


@pytest.mark.parametrize("case_id", ["pl_injection", "en_injection"])
def test_zero_candidate_analysis_is_idempotent_and_cannot_approve_instructions(case_id):
    async def scenario():
        async with fact_store() as state:
            _, item, staged = await staged_memory(state, CASES[case_id]["text"])
            assert not item.extraction.candidates and not staged.candidate_ids
            replay = await state.service.stage(StageBatch(sources=(item,)))
            assert replay.reused_sources == 1 and not replay.candidate_ids
            assert not await state.service.list_pending() and not await state.service.list_approved()
    run(scenario)


def test_unicode_offsets_and_exact_crlf_survive_postgres_and_review():
    async def scenario():
        async with fact_store() as state:
            prefix = "🧠 Zażółć gęślą jaźń e\u0301\r\n"
            case = copy.deepcopy(CASES["pl_flask"])
            case["text"] = prefix + case["text"]
            state.llm.cases = [case]
            memory, item, staged = await staged_memory(state, case["text"])
            candidate_id = staged.candidate_ids[0]
            await approve(state, candidate_id)
            async with open_owned_fact_memory(state.owned, config=state.config, intelligence=state.intelligence) as reopened:
                record = (await reopened.service.list_approved())[0]
                live = await reopened.storage.memory_document(memory.id)
                assert live.text == case["text"] and live.content_sha256 == item.source.content_sha256
                span = record.candidate.provenance.evidence[0]
                assert span.start == len(prefix) and span.start != len(prefix.encode("utf-16-le")) // 2
                assert live.text[span.start:span.end] == span.quote == CASES["pl_flask"]["text"]
    run(scenario)


@pytest.mark.parametrize("action", ["reject", "revoke"])
def test_relation_decisions_are_separate_terminal_and_idempotent(action):
    async def scenario():
        async with fact_store() as state:
            memories = [await create_memory(state, CASES[key]["text"]) for key in ("pl_flask", "pl_paraphrase")]
            docs = [await state.storage.memory_document(memory.id) for memory in memories]
            preview = await state.intelligence.preview(docs[0], (docs[1],))
            batch = StageBatch(sources=tuple(StageSource(source=source, extraction=extraction)
                for source, extraction in zip(docs, preview.extractions)), relations=preview.relations)
            staged = await state.service.stage(batch)
            for candidate_id in staged.candidate_ids:
                await approve(state, candidate_id)
            relation_id, expected = staged.relation_ids[0], 1
            if action == "revoke":
                await state.service.approve_relation(relation_id, principal=state.principal,
                    request_id=uuid4(), expected_version=1)
                expected = 2
            method = state.service.reject_relation if action == "reject" else state.service.revoke_relation
            request = uuid4()
            receipt = await method(relation_id, principal=state.principal, request_id=request, expected_version=expected)
            assert await method(relation_id, principal=state.principal, request_id=request, expected_version=expected) == receipt
            assert (await state.service.stage(batch)).relation_ids == staged.relation_ids
            assert len(await state.service.list_approved()) == 2
            assert not await state.service.list_relations(status="APPROVED")
            assert len(await state.service.list_relations(status="REJECTED" if action == "reject" else "REVOKED")) == 1
            assert len(await state.service.list_decisions(relation_id)) == expected
    run(scenario)
