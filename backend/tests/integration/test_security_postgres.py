"""Real PostgreSQL persistence/races/backup/auth. All mutations in owned DBs."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import secrets
import sys
import time
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_security_uvicorn import server
from backend.tests.test_security_transport import Runtime, HTTP, WS, no_private_work
from backend.tests.test_memory_ws import _start_message, _stop_message
from kulai_memory.app_factory import create_app
from kulai_memory.database_safety import database_snapshot_url, run_database_doctor, expected_alembic_heads
from kulai_memory.security import CredentialManager, Authenticator, DeviceStatus, Action, SecurityError
from kulai_memory.security.authentication import token_digest
from kulai_memory.security_persistence.postgres import PostgresSecurityStore
from kulai_memory.security_persistence.runtime import PostgresSecurityRuntime
from kulai_memory.security_persistence.migration import upgrade_security_with_verified_backup, security_migration_heads


async def credential(store):
    manager = CredentialManager(store)
    device = await manager.register_device(trusted=True)
    return manager, device, await manager.issue(device.id)


def test_persistence_adapter_recreation_trust_revocation_scopes_and_no_plaintext():
    async def scenario():
        async with owned_store() as (_, _, target, factory, store):
            manager, device, token = await credential(store)
            second_engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
            try:
                restarted = PostgresSecurityStore(async_sessionmaker(second_engine, expire_on_commit=False))
                assert (await Authenticator(restarted).authenticate(token.access_token)).device_id == device.id
                await manager.revoke(token.token_id)
                with pytest.raises(SecurityError):
                    await Authenticator(restarted).authenticate(token.access_token)
                scoped = await manager.issue(device.id, actions=frozenset({Action.MEMORY_READ}), project_id=uuid4())
                state = await restarted.lookup(token_digest(scoped.access_token))
                assert state.token.project_id is not None and state.token.actions == frozenset({Action.MEMORY_READ})
                await manager.revoke_device(device.id)
                assert (await restarted.lookup(token_digest(scoped.access_token))).device.status is DeviceStatus.REVOKED
                with pytest.raises(SecurityError):
                    await CredentialManager(restarted).trust_device(device.id)
                with pytest.raises(SecurityError):
                    await Authenticator(restarted).authenticate(scoped.access_token)
                async with factory() as session:
                    serialized = await session.scalar(sa.text("SELECT json_agg(t)::text FROM public.kulai_security_tokens t"))
                    assert token.access_token not in serialized and scoped.access_token not in serialized
                assert second_engine.pool.checkedout() == factory.kw["bind"].pool.checkedout() == 0
            finally:
                await second_engine.dispose()
    asyncio.run(scenario())


def test_atomic_rotation_and_revoke_device_races_across_connections():
    async def scenario():
        async with owned_store() as (_, _, _, factory, store):
            manager, device, token = await credential(store)
            results = await asyncio.gather(*(manager.rotate(token.access_token, device_id=device.id) for _ in range(8)), return_exceptions=True)
            successors = [r for r in results if not isinstance(r, Exception)]
            assert len(successors) == 1
            assert sum(isinstance(r, SecurityError) for r in results) == 7
            with pytest.raises(SecurityError):
                await Authenticator(store).authenticate(token.access_token)
            successor = successors[0]
            assert await Authenticator(store).authenticate(successor.access_token)
            results = await asyncio.gather(manager.revoke(successor.token_id),
                manager.rotate(successor.access_token, device_id=device.id), return_exceptions=True)
            with pytest.raises(SecurityError):
                await Authenticator(store).authenticate(successor.access_token)
            assert results[0] is None
            issued = await asyncio.gather(manager.issue(device.id), manager.revoke_device(device.id), return_exceptions=True)
            if not isinstance(issued[0], Exception):
                with pytest.raises(SecurityError):
                    await Authenticator(store).authenticate(issued[0].access_token)
            with pytest.raises(SecurityError):
                await manager.issue(device.id)
            assert factory.kw["bind"].pool.checkedout() == 0
    asyncio.run(scenario())


async def processes(owned, device, token, operations):
    children = []
    try:
        for operation in operations:
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "backend.tests.security_postgres_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            children.append(process)
            data = dict(database=owned.name, marker=owned.marker, device_id=str(device.id),
                        token_id=str(token.token_id), token=token.access_token, operation=operation)
            process.stdin.write((json.dumps(data) + "\n").encode()); await process.stdin.drain()
        for process in children:
            assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"READY"
        for process in children:
            process.stdin.write(b"GO\n"); await process.stdin.drain()
        results = []
        for process in children:
            output, errors = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0
            assert token.access_token.encode() not in output + errors
            assert errors == b""
            results.append(json.loads(output))
        return results
    finally:
        for process in children:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize("operations", [("rotate", "rotate"), ("rotate", "revoke"), ("issue", "revoke_device")])
def test_two_processes_share_transaction_locks(operations):
    async def scenario():
        async with owned_store() as (owned, _, _, factory, store):
            _, device, token = await credential(store)
            result = await processes(owned, device, token, operations)
            if operations == ("rotate", "rotate"):
                assert sum(r["ok"] for r in result) == 1
                async with factory() as session:
                    assert await session.scalar(sa.text("SELECT count(*) FROM public.kulai_security_tokens")) == 2
            elif operations == ("rotate", "revoke"):
                assert result[1]["ok"]
            else:
                assert result[1]["ok"]
                assert (await store.lookup(token_digest(token.access_token))).device.status is DeviceStatus.REVOKED
            with pytest.raises(SecurityError):
                await Authenticator(store).authenticate(token.access_token)
    asyncio.run(asyncio.wait_for(scenario(), 40))


@pytest.mark.parametrize("fault", ["before_commit", "after_rollback"])
def test_failed_rotation_rolls_back_and_closes_even_when_rollback_hook_fails(fault, caplog):
    async def scenario():
        async with owned_store() as (_, _, _, factory, store):
            manager, device, token = await credential(store)
            original = (await store.lookup(token_digest(token.access_token))).token
            replacement = replace(original, id=uuid4(), digest="a" * 64, issued_at=datetime.now(timezone.utc),
                                  expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
            failing = async_sessionmaker(factory.kw["bind"], expire_on_commit=False, info={"fault": True})
            def fail(session):
                if session.info.get("fault"):
                    raise RuntimeError(token.access_token)
            event = "before_commit" if fault == "before_commit" else "after_rollback"
            sa.event.listen(AsyncSession.sync_session_class, event, fail)
            try:
                if fault == "after_rollback":
                    replacement = replace(replacement, digest=original.digest)  # real unique violation after old revoke
                with pytest.raises(SecurityError) as caught:
                    await PostgresSecurityStore(failing).rotate_token(token.token_id, replacement, now=replacement.issued_at)
                assert caught.value.__cause__ is None and token.access_token not in str(caught.value)
            finally:
                sa.event.remove(AsyncSession.sync_session_class, event, fail)
            assert await Authenticator(store).authenticate(token.access_token)
            async with factory() as session:
                assert await session.scalar(sa.text("SELECT count(*) FROM public.kulai_security_tokens")) == 1
            assert factory.kw["bind"].pool.checkedout() == 0 and token.access_token not in caplog.text
    asyncio.run(scenario())


def test_cancelled_lookup_rolls_back_read_only_transaction_and_releases_pool():
    async def scenario():
        async with owned_store() as (_, _, _, factory, store):
            _, _, token = await credential(store)
            entered = asyncio.Event()
            class Blocked(AsyncSession):
                async def execute(self, statement, *args, **kwargs):
                    if str(statement).startswith("SELECT"):
                        entered.set()
                        await asyncio.Event().wait()
                    return await super().execute(statement, *args, **kwargs)
            blocked = PostgresSecurityStore(async_sessionmaker(factory.kw["bind"], class_=Blocked))
            task = asyncio.create_task(blocked.lookup(token_digest(token.access_token)))
            await entered.wait(); task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert factory.kw["bind"].pool.checkedout() == 0
            assert await Authenticator(store).authenticate(token.access_token)
    asyncio.run(scenario())


@pytest.mark.parametrize("tamper", ["constraint", "partial", "extra", "revision", "index"])
def test_strict_doctor_and_snapshot_reject_broken_security_schema(tamper):
    async def scenario():
        async with owned_store() as (_, _, target, factory, _):
            assert (await run_database_doctor(async_url=target.async_url, require_security=True)).ok
            sql = {"constraint": "ALTER TABLE public.kulai_security_tokens DROP CONSTRAINT ck_security_token_lifetime",
                   "partial": "DROP TABLE public.kulai_security_tokens CASCADE",
                   "extra": "ALTER TABLE public.kulai_security_tokens ADD COLUMN plaintext text",
                   "revision": "UPDATE public.kulai_security_version SET version_num='unknown'",
                   "index": "DROP INDEX public.ix_security_tokens_device"}[tamper]
            async with factory() as session:
                await session.execute(sa.text(sql)); await session.commit()
            assert not (await run_database_doctor(async_url=target.async_url)).ok
            with pytest.raises(Exception, match="snapshot could not be read safely"):
                await database_snapshot_url(target.async_url)
    asyncio.run(scenario())


def test_additive_guarded_migration_backup_restore_and_security_fingerprints(tmp_path):
    async def scenario():
        async with owned_store(security=False) as (owned, config, target, factory, store):
            from scripts.db_backup_restore_drill import seed_owned_source
            await seed_owned_source(owned, config=config, dimension=1024)
            before = await database_snapshot_url(target.async_url)
            assert (before.memories.count, before.vectors.count, before.tombstones.count) == (2, 1, 1)
            assert before.security is None
            assert (await run_database_doctor(async_url=target.async_url)).ok
            assert not (await run_database_doctor(async_url=target.async_url, require_security=True)).ok
            backup = await upgrade_security_with_verified_backup(tmp_path / "before-security.dump", config=config, app_env="test", owned=owned)
            after = await database_snapshot_url(target.async_url)
            assert after.memories == before.memories and after.vectors == before.vectors and after.tombstones == before.tombstones
            assert after.revision == before.revision == expected_alembic_heads() == ("kulai_memory_0005",)
            assert security_migration_heads() == after.security.revision == ("kulai_security_0003",)
            assert backup.source_snapshot == before and backup.path.is_file()
            manager, device, token = await credential(store)
            full = await database_snapshot_url(target.async_url)
            assert full.security.devices.count == full.security.tokens.count == 1
            from kulai_memory.backup_service import create_verified_backup, revalidate_verified_backup
            durable = await create_verified_backup(tmp_path / "with-security.dump", config=config, app_env="test", owned=owned)
            assert durable.source_snapshot == full  # actual pg_restore verified all security state
            await manager.revoke(token.token_id)
            revoked = await database_snapshot_url(target.async_url)
            assert revoked.security.tokens.sha256 != full.security.tokens.sha256
            assert revoked.memories == full.memories and revoked.vectors == full.vectors and revoked.tombstones == full.tombstones
            with pytest.raises(Exception, match="revalidation could not be completed"):
                await revalidate_verified_backup(durable, config=target)
    asyncio.run(scenario())


def test_managed_secure_endpoints_restart_audio_query_cost_and_revocation(capsys):
    async def scenario():
        async with owned_store() as (_, _, target, factory, store):
            manager, device, token = await credential(store)
            headers = {"Authorization": "Bearer " + token.access_token, "X-Kulai-Device-ID": str(device.id)}
            runtime = Runtime()
            instances, statements = [], []
            def security_factory():
                owned = PostgresSecurityRuntime(config_factory=lambda: target)
                original = owned._engine_factory
                def engine_factory(*args, **kwargs):
                    engine = original(*args, **kwargs)
                    sa.event.listen(engine.sync_engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
                    return engine
                owned._engine_factory = engine_factory
                instances.append(owned)
                return owned
            app = create_app(voice_runtime_factory=lambda: runtime, security_runtime_factory=security_factory)
            for name in ("list_memories", "get_memory", "ask_memory", "plan_speech", "transcribe", "transcribe_question", "ingest"):
                original_operation = getattr(runtime, name)
                async def no_security_session(_operation=original_operation, **kwargs):
                    assert instances[-1]._engine.pool.checkedout() == 0
                    return await _operation(**kwargs)
                setattr(runtime, name, no_security_session)
            for restarted in (False, True):
                async with server(app) as port:
                    base = f"http://127.0.0.1:{port}"
                    async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                        for method, path in HTTP:
                            assert (await client.request(method, path, content=b"private-invalid-body")).status_code == 401
                        for path in WS:
                            with pytest.raises(InvalidStatus) as caught:
                                async with connect(f"ws://127.0.0.1:{port}{path}"):
                                    pytest.fail()
                            assert caught.value.response.status_code == 403
                        if not restarted:
                            no_private_work(runtime)
                        assert (await client.get("/api/memories", headers=headers)).status_code == 200
                        assert (await client.get(HTTP[1][1], headers=headers)).status_code == 404
                        assert (await client.post("/api/assistant/ask", headers=headers, json={"query": "Synthetic"})).status_code == 200
                        assert (await client.post("/api/assistant/speech-plan", headers=headers, json={"answer": "Synthetic answer"})).status_code == 200
                        if not restarted:
                            statements.clear(); frames = 128; started = time.perf_counter()
                            async with connect(f"ws://127.0.0.1:{port}/ws/memory", additional_headers=headers) as ws:
                                await ws.send(json.dumps(_start_message(uuid4()))); await ws.recv()
                                for _ in range(frames):
                                    await ws.send(b"\0\0" * 512)
                                await ws.send(json.dumps(_stop_message()))
                                events = [json.loads(await ws.recv()) for _ in range(3)]
                                assert events[-1]["type"] == "memory.saved"
                            duration = (time.perf_counter() - started) * 1000
                            lookups = sum("JOIN public.kulai_security_devices" in s for s in statements)
                            assert lookups == frames + 3 and len(statements) == 2 * lookups
                            metric = f"security.audio.frames={frames};lookups={lookups};sql={len(statements)};elapsed_ms={duration:.2f};pool_checkout={instances[-1]._engine.pool.checkedout()}"
                            with capsys.disabled():
                                print(metric)
                            async with connect(f"ws://127.0.0.1:{port}/ws/assistant", additional_headers=headers) as ws:
                                from backend.tests.test_server_transport import start, stop
                                await ws.send(json.dumps(start())); await ws.recv(); await ws.send(b"\0\0" * 10)
                                await ws.send(json.dumps(stop()))
                                events = [json.loads(await ws.recv()) for _ in range(3)]
                                assert events[-1]["type"] == "assistant.completed"
                        assert instances[-1]._engine.pool.checkedout() == 0
                assert not instances[-1].ready and instances[-1]._engine is None
            await manager.revoke(token.token_id)
            async with server(app) as port:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    for method, path in HTTP:
                        assert (await client.request(method, path, headers=headers)).status_code == 401
                    for path in WS:
                        with pytest.raises(InvalidStatus):
                            async with connect(f"ws://127.0.0.1:{port}{path}", additional_headers=headers):
                                pytest.fail()
            assert len(instances) == runtime.startup_count == runtime.shutdown_count == 3
            assert token.access_token not in capsys.readouterr().out
    asyncio.run(asyncio.wait_for(scenario(), 40))


def test_unmigrated_secure_startup_denies_without_ai_and_local_dev_still_works():
    async def scenario():
        async with owned_store(security=False) as (_, _, target, _, _):
            runtime = Runtime()
            app = create_app(voice_runtime_factory=lambda: runtime,
                security_runtime_factory=lambda: PostgresSecurityRuntime(config_factory=lambda: target))
            async with server(app) as port:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    assert (await client.get("/health")).json() == {"status": "ok"}
                    assert (await client.get("/api/memories")).status_code == 401
            no_private_work(runtime)
            assert runtime.startup_count == runtime.shutdown_count == 0
            from backend.tests.local_transport import create_local_app
            local = create_local_app(voice_runtime_factory=lambda: runtime)
            async with server(local) as port:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    assert (await client.get("/api/memories")).status_code == 200
            assert runtime.startup_count == runtime.shutdown_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["invalid", "expired", "revoked", "revoked_device", "other_device", "malformed", "project_scope", "storage_failure"])
def test_postgres_denials_all_private_routes_without_memory_or_ai(state, capsys):
    async def scenario():
        async with owned_store() as (_, _, target, factory, store):
            manager, device, token = await credential(store)
            headers = {"Authorization": "Bearer " + token.access_token, "X-Kulai-Device-ID": str(device.id)}
            status = 401
            if state == "invalid":
                headers["Authorization"] = "Bearer " + secrets.token_urlsafe(32)
            elif state == "expired":
                record = (await store.lookup(token_digest(token.access_token))).token
                record = replace(record, id=uuid4(), digest=token_digest(secrets.token_urlsafe(32)),
                    issued_at=datetime.now(timezone.utc) - timedelta(hours=2),
                    expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
                # Preserve a known test secret for the intentionally expired record.
                secret = secrets.token_urlsafe(32)
                record = replace(record, digest=token_digest(secret))
                await store.create_token(record)
                headers["Authorization"] = "Bearer " + secret
            elif state == "revoked":
                await manager.revoke(token.token_id)
            elif state == "revoked_device":
                await manager.revoke_device(device.id)
            elif state == "other_device":
                headers["X-Kulai-Device-ID"] = str((await manager.register_device(trusted=True)).id)
            elif state == "malformed":
                headers["Authorization"] = "Bearer\t" + token.access_token
            elif state == "project_scope":
                scoped = await manager.issue(device.id, project_id=uuid4())
                headers["Authorization"] = "Bearer " + scoped.access_token
                status = 403
            runtime = Runtime()
            app = create_app(voice_runtime_factory=lambda: runtime,
                security_runtime_factory=lambda: PostgresSecurityRuntime(config_factory=lambda: target))
            async with server(app) as port:
                if state == "storage_failure":
                    async with factory() as session:
                        await session.execute(sa.text("DROP TABLE public.kulai_security_tokens CASCADE")); await session.commit()
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    for method, path in HTTP:
                        result = await client.request(method, path, headers=headers, content=b"PRIVATE_INVALID_BODY")
                        assert result.status_code == status
                        assert result.json() == {"detail": "Authentication required." if status == 401 else "Access denied."}
                    for path in WS:
                        with pytest.raises(InvalidStatus) as caught:
                            async with connect(f"ws://127.0.0.1:{port}{path}", additional_headers=headers):
                                pytest.fail()
                        assert caught.value.response.status_code == 403
                no_private_work(runtime)
            assert token.access_token not in capsys.readouterr().out
    asyncio.run(scenario())


def test_guarded_ddl_failure_rolls_back_version_tables_and_canonical_state(tmp_path, monkeypatch):
    from kulai_memory.security_persistence import migration
    async def scenario():
        async with owned_store(security=False) as (owned, config, target, _, _):
            before = await database_snapshot_url(target.async_url)
            original = migration._upgrade
            def fail(connection):
                original(connection)
                raise RuntimeError("PRIVATE_MIGRATION_SENTINEL")
            monkeypatch.setattr(migration, "_upgrade", fail)
            with pytest.raises(Exception, match="upgrade failed safely"):
                await upgrade_security_with_verified_backup(tmp_path / "rollback-security.dump", config=config, app_env="test", owned=owned)
            assert await database_snapshot_url(target.async_url) == before
            assert (await run_database_doctor(async_url=target.async_url)).ok
            assert (tmp_path / "rollback-security.dump").is_file()
    asyncio.run(scenario())
