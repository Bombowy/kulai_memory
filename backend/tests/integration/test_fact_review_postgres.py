"""Real Qt -> QThread -> FactMemoryService -> owned PostgreSQL decisions."""

import asyncio
import os
import time
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtTest import QTest
from PySide6.QtCore import QTimer, QThread
from PySide6.QtWidgets import QApplication

from backend.tests.security_postgres_fixtures import owned_store
from kulai_memory.application.fact_memory import FactMemoryError
from kulai_memory.application.intelligence import MemoryIntelligenceService
from kulai_memory.database_safety import database_exists, database_snapshot_url
from kulai_memory.deletion_persistence import delete_memory
from kulai_memory.desktop.fact_review.backend import FactReviewBackend
from kulai_memory.desktop.fact_review.demo import SyntheticLLM, SyntheticBGE, demo_backend, seed_demo
from kulai_memory.desktop.fact_review.models import Intent, Query
from kulai_memory.desktop.fact_review.window import FactReviewWindow
from kulai_memory.fact_persistence.migration import upgrade_owned_facts
from kulai_memory.fact_persistence.owned import open_owned_fact_memory


def wait_for(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        QApplication.processEvents()
        time.sleep(0.01)  # Release the GIL for the Python asyncio QThread.
    assert condition(), "Qt operation timed out"


@contextmanager
def review_database():
    app = QApplication.instance() or QApplication([])
    with asyncio.Runner() as runner:
        context = owned_store(security=False)
        owned, config, target, factory, _ = runner.run(context.__aenter__())
        try:
            runner.run(upgrade_owned_facts(owned, config=config))
            async def prepare():
                llm, bge = SyntheticLLM(), SyntheticBGE()
                intelligence = MemoryIntelligenceService(llm_provider=llm, embedding_provider=bge)
                async with open_owned_fact_memory(owned, config=config, intelligence=intelligence) as local:
                    await seed_demo(local, factory, intelligence)
            runner.run(prepare())
            boxes = []
            @asynccontextmanager
            async def backend_factory():
                llm, bge = SyntheticLLM(), SyntheticBGE()
                intelligence = MemoryIntelligenceService(llm_provider=llm, embedding_provider=bge)
                try:
                    async with open_owned_fact_memory(owned, config=config, intelligence=intelligence) as local:
                        backend = FactReviewBackend(local, database_name=owned.name)
                        boxes.append((backend, intelligence))
                        yield backend
                finally:
                    await llm.aclose()
                    await bge.aclose()
            yield app, backend_factory, boxes, owned, config, target, runner
        finally:
            runner.run(context.__aexit__(None, None, None))
            assert not runner.run(database_exists(owned.name, config=config))


@contextmanager
def window_for(factory, *, page_size=20):
    window = FactReviewWindow(backend_factory=factory, page_size=page_size)
    window.show()
    try:
        wait_for(lambda: window._view is not None or window._finished)
        assert not window._finished, window.message.text()
        yield window
    finally:
        window.close()
        wait_for(lambda: window._finished)
        assert window.worker.clean_shutdown


def on_worker(window, create):
    future = asyncio.run_coroutine_threadsafe(create(), window.worker._loop)
    wait_for(future.done)
    return future.result()


def loaded(window):
    wait_for(lambda: not window._busy and window._view is not None)


def filter_by(window, name, value):
    combo = getattr(window, name)
    combo.setCurrentIndex(combo.findData(value))
    loaded(window)


def select(window, index=0):
    loaded(window)
    assert window.table.rowCount() > index, (window._view.total_count, window.message.text())
    window.table.clearSelection()
    window.table.selectRow(index)
    try:
        wait_for(lambda: not window._busy and window._detail is not None)
    except AssertionError:
        raise AssertionError((window._busy, window._selected, window.table.rowCount(), window.message.text())) from None
    return window._detail


def test_pending_filters_pagination_and_real_approve_reject_revoke_reopen():
    with review_database() as (app, factory, boxes, owned, config, target, runner):
        before = runner.run(database_snapshot_url(target.async_url))
        with window_for(factory, page_size=2) as window:
            assert window._view.pending_count == 4 and len(window._view.rows) == 2
            page_one = {r.id for r in window._view.rows}
            window.next.click(); loaded(window)
            assert len(window._view.rows) == 2 and not page_one & {r.id for r in window._view.rows}
            window.previous.click(); loaded(window)
            assert page_one == {r.id for r in window._view.rows}
            filter_by(window, "type_filter", "hypothesis")
            assert window._view.total_count == 0
            filter_by(window, "type_filter", "plan")
            assert window._view.total_count == 1
            detail = select(window)
            approved_id = detail.id
            assert detail.snapshots[0].record.candidate.fact.information_type == "plan"
            assert window.left.source.extraSelections()
            window.approve.click()
            request_id = window._pending_intent.request_id
            window.approve.click(); window._decide("approve")
            wait_for(lambda: window._pending_intent is None and not window._busy)
            loaded(window)
            backend = boxes[-1][0]
            receipt = on_worker(window, lambda: backend._local.storage.receipt(request_id))
            assert receipt.action == "approve"
            assert len(on_worker(window, lambda: backend._local.service.list_decisions(approved_id))) == 1
            filter_by(window, "type_filter", "preference")
            rejected_id = select(window).id
            window.reject.click()
            wait_for(lambda: window._pending_intent is None and not window._busy)
            loaded(window)
        # New QThread, new pool, new principal, SAME durable owned DB.
        with window_for(factory) as window:
            filter_by(window, "status_filter", "APPROVED")
            assert [r.id for r in window._view.rows] == [approved_id]
            select(window)
            assert "approve" in window.history.toPlainText()
            window.revoke.click()
            wait_for(lambda: window._pending_intent is None and not window._busy)
            loaded(window)
            filter_by(window, "status_filter", "REVOKED")
            assert select(window).id == approved_id
            assert "revoke" in window.history.toPlainText() and not window.approve.isEnabled()
            filter_by(window, "status_filter", "REJECTED")
            assert select(window).id == rejected_id
            assert not window.approve.isEnabled()
        assert runner.run(database_snapshot_url(target.async_url)) == before


@pytest.mark.parametrize("mutation", ["edit", "archive", "restore", "delete"])
def test_open_panel_is_scrubbed_after_source_lifecycle(mutation, caplog):
    with review_database() as (_, factory, boxes, *rest):
        with window_for(factory) as window:
            filter_by(window, "type_filter", "plan")
            detail = select(window)
            memory_id = detail.snapshots[0].source.memory_id
            backend = boxes[-1][0]
            async def mutate():
                storage = backend._local.storage
                if mutation == "delete":
                    await delete_memory(memory_id=memory_id, session_factory=async_sessionmaker(storage._engine, expire_on_commit=False))
                else:
                    async with storage._tx() as repo:
                        if mutation == "edit":
                            await repo.command("UPDATE memories SET content='changed synthetic',revision=revision+1 WHERE id=:id", {"id": memory_id})
                        else:
                            await repo.command("UPDATE memories SET archived_at=clock_timestamp() WHERE id=:id", {"id": memory_id})
                            if mutation == "restore":
                                await repo.command("UPDATE memories SET archived_at=NULL WHERE id=:id", {"id": memory_id})
            on_worker(window, mutate)
            # Automatic polling, with no explicit user REFRESH.
            wait_for(lambda: window._view is not None and window._view.total_count == 0 and not window.left.source.toPlainText())
            assert window._detail is None and not window.left.claim.toPlainText() and not window.history.toPlainText()
            window._refresh(); loaded(window)
            filter_by(window, "type_filter", None)
            assert window._view.total_count == 3
            filter_by(window, "type_filter", "plan")
            assert not window._view.rows and not window.left.source.toPlainText()
    assert "W przyszłym roku" not in caplog.text


def test_relation_review_requires_two_approved_endpoints_and_never_deletes_memories():
    with review_database() as (_, factory, boxes, *rest):
        with window_for(factory) as window:
            filter_by(window, "mode", "relations")
            filter_by(window, "relation_filter", "possible_supersession")
            assert window._view.total_count == 1
            detail = select(window)
            assert len(detail.snapshots) == 2
            assert window.left.source.toPlainText() and window.right.source.toPlainText()
            assert not window.approve.isEnabled()
            relation_id = detail.id
            backend = boxes[-1][0]
            async def approve_both():
                for snapshot in detail.snapshots:
                    await backend._local.service.approve(snapshot.record.id, principal=backend._local.principal,
                        request_id=uuid4(), expected_version=1)
            on_worker(window, approve_both)
            window._refresh(); loaded(window)
            assert window._detail is not None and window.approve.isEnabled()
            window.approve.click()
            wait_for(lambda: window._pending_intent is None and not window._busy)
            loaded(window)
            filter_by(window, "status_filter", "APPROVED")
            assert select(window).id == relation_id
            async def check():
                assert len(await backend._local.service.list_approved()) == 2
                async with backend._local.storage._tx(read_only=True) as repo:
                    assert (await repo.one("SELECT count(*) AS n FROM memories WHERE archived_at IS NULL"))["n"] == 4
            on_worker(window, check)


def test_unavailable_verifier_pg_error_and_concurrent_decision_clear_private_view(caplog):
    with review_database() as (_, factory, boxes, *rest):
        with window_for(factory) as window:
            detail = select(window)
            backend, intelligence = boxes[-1]
            original = intelligence.verify_candidates
            async def unavailable(*args):
                raise RuntimeError("PRIVATE_PROVIDER_PAYLOAD")
            intelligence.verify_candidates = unavailable
            window.approve.click()
            wait_for(lambda: window._pending_intent is None and not window._busy)
            assert "Weryfikator" in window.message.text()
            intelligence.verify_candidates = original
            loaded(window)
            # Observe one version, let another genuine decision win, then click.
            detail = select(window)
            async def other():
                return await backend._local.service.reject(detail.id, principal=backend._local.principal,
                    request_id=uuid4(), expected_version=detail.lock_version)
            on_worker(window, other)
            window.approve.click()
            wait_for(lambda: window._pending_intent is None and not window._busy)
            assert "Inna decyzja" in window.message.text()
            loaded(window)
            select(window)
            original_read = backend.read
            async def broken(query):
                raise RuntimeError("PRIVATE_DB_PAYLOAD")
            backend.read = broken
            window._refresh()
            wait_for(lambda: not window._busy)
            assert window._detail is None and not window.left.source.toPlainText() and not window.history.toPlainText()
            backend.read = original_read
    assert "PRIVATE_PROVIDER_PAYLOAD" not in caplog.text and "PRIVATE_DB_PAYLOAD" not in caplog.text


def test_close_cancels_verifier_and_releases_connections():
    with review_database() as (_, factory, boxes, *rest):
        with window_for(factory) as window:
            select(window)
            backend, intelligence = boxes[-1]
            started = []
            async def slow(*args):
                assert backend._local.storage._engine.pool.checkedout() == 0
                started.append(True)
                await asyncio.Event().wait()
            intelligence.verify_candidates = slow
            window.approve.click()
            wait_for(lambda: bool(started))
            window.close()
            assert not window.left.source.toPlainText() and window._detail is None
            wait_for(lambda: window._finished)
            assert backend._local.storage._engine.pool.checkedout() == 0
            assert intelligence._llm.closed and intelligence._bge.closed


def test_demo_launcher_context_creates_and_drops_exact_owned_database():
    async def scenario():
        # Explicit opt-in and local safety reused before provisioning.
        from backend.tests.integration.test_memory_postgres import _require_opt_in
        from kulai_memory.database_safety import database_config
        _require_opt_in()
        async with demo_backend() as backend:
            name = backend.database_name
            view = await backend.read(Query())
            assert view.pending_count == 4
        assert not await database_exists(name, config=database_config())
    asyncio.run(scenario())


@pytest.mark.parametrize("delete_after_commit", [False, True])
def test_unknown_commit_ack_resolves_same_request_without_restoring_deleted_content(delete_after_commit):
    with review_database() as (_, factory, boxes, *rest):
        with window_for(factory) as window:
            filter_by(window, "type_filter", "plan")
            detail = select(window)
            backend = boxes[-1][0]
            storage = backend._local.storage
            original_decide, original_receipt = storage.decide, storage.receipt
            outage = []
            async def lost_ack(**kwargs):
                await original_decide(**kwargs)
                outage.append(True)
                raise FactMemoryError()
            async def receipt_unavailable(key):
                if outage:
                    raise FactMemoryError()
                return await original_receipt(key)
            storage.decide, storage.receipt = lost_ack, receipt_unavailable
            window.approve.click()
            request = window._pending_intent.request_id
            wait_for(lambda: not window._busy and window._pending_intent is not None and window.resolve.isEnabled())
            loaded(window)
            assert window._pending_intent.request_id == request and not window.left.source.toPlainText()
            if delete_after_commit:
                async def remove():
                    await delete_memory(memory_id=detail.snapshots[0].source.memory_id,
                        session_factory=async_sessionmaker(storage._engine, expire_on_commit=False))
                on_worker(window, remove)
            storage.decide, storage.receipt = original_decide, original_receipt
            window.resolve.click()
            wait_for(lambda: window._pending_intent is None and not window._busy)
            loaded(window)
            async def count():
                async with storage._tx(read_only=True) as repo:
                    return (await repo.one("SELECT count(*) AS n FROM kulai_fact_decisions WHERE request_id=:id", {"id": request}))["n"]
            assert on_worker(window, count) == 1
            filter_by(window, "status_filter", "APPROVED")
            assert window._view.total_count == (0 if delete_after_commit else 1)
            if delete_after_commit:
                assert not window._view.rows and not window.left.source.toPlainText() and not window.history.toPlainText()


def test_close_during_demo_provisioning_still_cleans_its_generated_database():
    from backend.tests.integration.test_memory_postgres import _require_opt_in
    from kulai_memory.database_safety import database_config
    _require_opt_in()
    app = QApplication.instance() or QApplication([])
    names = []
    @asynccontextmanager
    async def factory():
        async with demo_backend() as backend:
            names.append(backend.database_name)
            yield backend
    window = FactReviewWindow(backend_factory=factory)
    window.show()
    window.close()
    wait_for(lambda: window._finished)
    assert window.worker.clean_shutdown and len(names) == 1
    assert not asyncio.run(database_exists(names[0], config=database_config()))


def test_actual_launcher_event_loop_window_and_cleanup(monkeypatch, tmp_path):
    from backend.tests.integration.test_memory_postgres import _require_opt_in
    from kulai_memory.database_safety import database_config
    from kulai_memory.desktop.fact_review import window as window_module
    from scripts import fact_review_demo
    _require_opt_in()
    app = QApplication.instance() or QApplication([])
    windows, captured = [], []
    original = window_module.FactReviewWindow
    class ObservedWindow(original):
        def __init__(self):
            super().__init__()
            windows.append(self)
        def _display(self, view):
            assert QThread.currentThread() == app.thread()
            super()._display(view)
    monkeypatch.setattr(window_module, "FactReviewWindow", ObservedWindow)
    timer = QTimer()
    timer.setInterval(20)
    def inspect():
        if not windows:
            return
        window = windows[0]
        if window._busy or window._view is None:
            return
        if window._detail is None:
            window.table.selectRow(0)
        else:
            image_path = tmp_path / "synthetic_fact_review.png"
            captured.append((window.worker._ready, window.worker._loop is not None,
                             window._detail.snapshots[0].source.memory_id, window.database.text()))
            window.grab().save(str(image_path))
            timer.stop()
            window.close()
    timer.timeout.connect(inspect)
    timer.start()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(lambda: windows and windows[0].close())
    timeout.start(15000)
    try:
        assert fact_review_demo.main([]) == 0
    finally:
        timer.stop(); timeout.stop()
    assert captured and (tmp_path / "synthetic_fact_review.png").is_file()
    name = captured[0][3].split()[2]
    assert not asyncio.run(database_exists(name, config=database_config()))


def test_unicode_repeated_evidence_roundtrip_and_deletion_of_selected_source():
    from copy import deepcopy
    from backend.tests.test_memory_intelligence import GoldenLLM, CASES
    from kulai_memory.application import Memory
    from kulai_memory.application.fact_memory import StageBatch, StageSource
    from kulai_memory.persistence import PostgresMemoryRepository
    with review_database() as (_, factory, boxes, *rest):
        with window_for(factory) as window:
            backend, intelligence = boxes[-1]
            async def stage_unicode():
                case = deepcopy(CASES["pl_flask"])
                case["text"] = "🧠 Zażółć e\u0301\r\n" + case["text"] + "\r\n" + case["text"]
                intelligence._llm = GoldenLLM(cases=[case])
                factory = async_sessionmaker(backend._local.storage._engine, expire_on_commit=False)
                async with factory() as session:
                    async with session.begin():
                        memory = await PostgresMemoryRepository(db=session).create(Memory(content=case["text"]))
                source = await backend._local.storage.memory_document(memory.id)
                extraction = await intelligence.extract(source)
                staged = await backend._local.service.stage(StageBatch(sources=(StageSource(source=source, extraction=extraction),)))
                return memory.id, staged.candidate_ids[0]
            memory_id, candidate_id = on_worker(window, stage_unicode)
            window._refresh(); loaded(window)
            index = next(i for i, row in enumerate(window._view.rows) if row.id == candidate_id)
            detail = select(window, index)
            assert len(detail.snapshots[0].record.candidate.provenance.evidence) == 2
            selections = window.left.source.extraSelections()
            assert len(selections) == 2 and selections[0].cursor.selectionStart() != selections[1].cursor.selectionStart()
            assert "🧠" in window.left.source.toPlainText()
            async def remove():
                await delete_memory(memory_id=memory_id,
                    session_factory=async_sessionmaker(backend._local.storage._engine, expire_on_commit=False))
            on_worker(window, remove)
            wait_for(lambda: window._detail is None and not window.left.source.toPlainText() and window._view is not None)
            assert not window.left.source.extraSelections()
            assert candidate_id not in {row.id for row in window._view.rows}
