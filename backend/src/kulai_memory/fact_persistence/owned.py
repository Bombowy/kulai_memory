"""Trusted local test adapter. Every transaction verifies its actual DB and ownership marker.

This host adapter owns atomic transaction boundaries. The SQL repository never
commits. No HTTP route, startup hook, arbitrary URL argument or main mode exists.
"""

import getpass
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ..application.fact_memory import FactMemoryError, FactMemoryService, StageBatch, StageSource
from ..application.fact_memory.models import DecisionReceipt, StagedBatch, StoredRelation
from ..application.intelligence import SourceDocument
from ..application.intelligence.models import RelationSuggestion
from ..database_safety import (
    SAFE_LOCAL_APP_ENVS, database_config_for_database, database_host_is_loopback, require_owned_database,
    validate_owned_database_name,
)
from ..settings import get_settings
from .repository import CANDIDATE_FROM, VALID_SOURCE, PostgresFactRepository, memory_source_id, memory_document_id
from .schema_v2 import FACT_HEAD


@dataclass(frozen=True)
class _LocalPrincipal:
    reviewer_id: str
    proof: object = field(repr=False)


class _LocalAuthority:
    def __init__(self):
        self._proof = object()
        self.principal = _LocalPrincipal("local-test:" + getpass.getuser(), self._proof)

    def require(self, principal):
        # Neither strings, JSON claims nor a capability minted by another scope are authority.
        if principal is not self.principal or principal.proof is not self._proof:
            raise FactMemoryError("forbidden")
        return self.principal.reviewer_id


class OwnedFactStorage:
    def __init__(self, engine, owned):
        # Prefix validation is mandatory even for direct trusted-Python construction.
        validate_owned_database_name(owned.name, source_database="__fact_main_forbidden__", kind=owned.kind)
        self._engine, self._owned = engine, owned

    @asynccontextmanager
    async def _tx(self, *, read_only=False):
        try:
            async with self._engine.connect() as connection:
                transaction = await connection.begin()
                committing = False
                try:
                    if read_only:
                        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                    await connection.execute(text("SET LOCAL statement_timeout='10s'"))
                    await connection.execute(text("SET LOCAL lock_timeout='5s'"))
                    await connection.execute(text("SET LOCAL search_path TO pg_catalog,public"))
                    actual, marker = (await connection.execute(text("""SELECT current_database(),
                        shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"""))).one()
                    if actual != self._owned.name or marker != self._owned.marker:
                        raise FactMemoryError("owned_database_required")
                    if not await connection.scalar(text("SELECT to_regclass('public.kulai_facts_version') IS NOT NULL")):
                        raise FactMemoryError("schema_required")
                    heads = tuple((await connection.execute(text("SELECT version_num FROM public.kulai_facts_version"))).scalars())
                    if heads != (FACT_HEAD,):
                        raise FactMemoryError("schema_required")
                    yield PostgresFactRepository(connection)
                    if read_only:
                        await transaction.rollback()
                    else:
                        committing = True
                        await transaction.commit()
                        committing = False
                except BaseException:
                    if committing:
                        # A failed COMMIT can leave the driver's transaction state uncertain.
                        # Discard the connection; never put an uncommitted transaction back in the pool.
                        await connection.invalidate()
                    elif transaction.is_active:
                        await transaction.rollback()
                    raise
        except FactMemoryError:
            raise
        except Exception:
            raise FactMemoryError() from None

    async def memory_document(self, memory_id, *, speaker_id=None):
        if not isinstance(memory_id, UUID):
            raise FactMemoryError("invalid_input")
        async with self._tx(read_only=True) as repo:
            row = await repo.memory(memory_id)
            if row is None or row["archived_at"] is not None:
                raise FactMemoryError("source_invalid")
            return SourceDocument(source_id=memory_document_id(memory_id, row["revision"], row["token"]), text=row["content"],
                                  memory_id=memory_id, revision=row["revision"], created_at=row["created_at"], speaker_id=speaker_id)

    async def register_local(self, key, content, *, speaker_id=None):
        if not isinstance(key, UUID):
            raise FactMemoryError("invalid_input")
        try:
            document = SourceDocument(source_id=str(key), text=content, speaker_id=speaker_id)
        except Exception:
            raise FactMemoryError("invalid_input") from None
        async with self._tx() as repo:
            await repo.command("SELECT pg_advisory_xact_lock(:key)", {"key": int.from_bytes(key.bytes[:8], signed=True)})
            if await repo.retired(key):
                raise FactMemoryError("source_invalid")
            source = await repo.source(key, lock=True)
            if source is None:
                await repo.command("INSERT INTO public.kulai_fact_sources(id,local_key,local_text) VALUES(:id,:id,:text)", {"id": key, "text": content})
            elif source["memory_id"] is not None:
                raise FactMemoryError("invalid_input")
            elif source["local_text"] != content:
                await repo.invalidate(key)
                await repo.command("""UPDATE public.kulai_fact_sources SET local_text=:text,
                    local_revision=local_revision+1,local_token=gen_random_uuid() WHERE id=:id""", {"id": key, "text": content})
        return document

    async def forget_local(self, key):
        if not isinstance(key, UUID):
            raise FactMemoryError("invalid_input")
        async with self._tx() as repo:
            source = await repo.source(key, lock=True)
            if source is None:
                return
            if source["memory_id"] is not None:
                raise FactMemoryError("invalid_input")
            await repo.command("DELETE FROM public.kulai_fact_sources WHERE id=:id", {"id": key})

    async def reconcile_memory(self, memory_id):
        """Record observed invalidation. Read filters remain safe even before reconciliation."""
        if not isinstance(memory_id, UUID):
            raise FactMemoryError("invalid_input")
        async with self._tx() as repo:
            current = await repo.memory(memory_id, lock=True)
            source_id = memory_source_id(memory_id)
            source = await repo.source(source_id, lock=True)
            if source is not None:
                await repo.invalidate(source_id,
                    current_token=current["token"] if current and current["archived_at"] is None else None,
                    reason="source_archived" if current and current["archived_at"] else "source_changed")

    async def stage(self, batch):
        async with self._tx() as repo:
            # Canonical locks precede source/candidate locks, consistent with Memory DELETE.
            for memory_id in sorted({i.source.memory_id for i in batch.sources if i.source.memory_id}, key=str):
                await repo.memory(memory_id, lock=True)
            source_ids = [memory_source_id(i.source.memory_id) if i.source.memory_id else UUID(i.source.source_id)
                          for i in batch.sources]
            if len(set(source_ids)) != len(source_ids):
                raise FactMemoryError("invalid_input")
            for source_id in sorted(source_ids, key=str):
                await repo.source(source_id, lock=True)
            ids, mapping, reused = [], {}, 0
            for item in batch.sources:
                staged, matched, existing = await repo.stage_source(item)
                ids.extend(staged); mapping.update(matched); reused += int(existing)
            relations, skipped = [], 0
            for suggestion in batch.relations:
                if suggestion.relation not in {"exact_duplicate", "semantic_duplicate", "related", "possible_conflict", "possible_supersession"}:
                    skipped += 1
                    continue
                if suggestion.left_id not in mapping or suggestion.right_id not in mapping:
                    skipped += 1  # A rerun with changed claims cannot attach new relations to old approved claims.
                    continue
                relation = await repo.one("""INSERT INTO public.kulai_fact_relations(id,left_id,right_id,relation,payload)
                    VALUES(:id,:left,:right,:kind,CAST(:payload AS jsonb))
                    ON CONFLICT(left_id,right_id,relation) DO NOTHING RETURNING id""",
                    {"id": uuid4(), "left": mapping[suggestion.left_id], "right": mapping[suggestion.right_id],
                     "kind": suggestion.relation, "payload": suggestion.model_dump_json()})
                if relation is None:
                    relation = await repo.one("""SELECT id FROM public.kulai_fact_relations
                        WHERE left_id=:left AND right_id=:right AND relation=:kind""",
                        {"left": mapping[suggestion.left_id], "right": mapping[suggestion.right_id], "kind": suggestion.relation})
                relations.append(relation["id"])
            result = StagedBatch(candidate_ids=tuple(ids), relation_ids=tuple(relations), reused_sources=reused, skipped_relations=skipped)
        return result  # Return only after commit acknowledgement.

    async def get_candidate(self, candidate_id):
        async with self._tx(read_only=True) as repo:
            row = await repo.candidate_row(candidate_id)
            return repo.record(row) if row else None

    async def review_snapshot(self, candidate_id):
        async with self._tx(read_only=True) as repo:
            return await repo.snapshot(candidate_id)

    async def list_candidates(self, *, status, limit, after_id):
        async with self._tx(read_only=True) as repo:
            rows = await repo.rows("SELECT c.*," + f"({VALID_SOURCE}) AS source_valid " + CANDIDATE_FROM +
                f" WHERE c.status=:status AND ({VALID_SOURCE})" +
                (" AND c.id>:after" if after_id else "") + " ORDER BY c.id LIMIT :limit",
                {"status": status, "limit": limit, "after": after_id})
            return tuple(repo.record(row) for row in rows)

    async def receipt(self, request_id):
        async with self._tx(read_only=True) as repo:
            return await repo.receipt(request_id)

    async def _lock_candidates(self, repo, ids):
        sources = await repo.rows("""SELECT DISTINCT s.id,s.memory_id FROM public.kulai_fact_candidates c
            JOIN public.kulai_fact_source_versions v ON v.id=c.source_version_id
            JOIN public.kulai_fact_sources s ON s.id=v.source_id WHERE c.id=ANY(CAST(:ids AS uuid[]))""", {"ids": ids})
        for memory_id in sorted({s["memory_id"] for s in sources if s["memory_id"]}, key=str):
            await repo.memory(memory_id, lock=True)
        for source in sorted(sources, key=lambda s: str(s["id"])):
            await repo.source(source["id"], lock=True)
        return await repo.rows("SELECT * FROM public.kulai_fact_candidates WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE", {"ids": ids})

    async def decide(self, *, target_id, kind, action, reviewer_id, request_id, expected_version, snapshot):
        async with self._tx() as repo:
            await repo.command("SELECT pg_advisory_xact_lock(:key)", {"key": int.from_bytes(request_id.bytes[:8], signed=True)})
            prior = await repo.receipt(request_id)
            if prior is not None:
                if (prior.target_id, prior.target_kind, prior.action, prior.reviewer_id, prior.expected_version) != (
                    target_id, kind, action, reviewer_id, expected_version,
                ):
                    raise FactMemoryError("idempotency_conflict")
                return prior
            if kind == "candidate":
                await self._lock_candidates(repo, [target_id])
                row = await repo.candidate_row(target_id)
            else:
                row = await repo.one("SELECT * FROM public.kulai_fact_relations WHERE id=:id", {"id": target_id})
                if row is not None:
                    await self._lock_candidates(repo, [row["left_id"], row["right_id"]])
                    row = await repo.one("SELECT * FROM public.kulai_fact_relations WHERE id=:id FOR UPDATE", {"id": target_id})
            if row is None:
                raise FactMemoryError("not_found")
            if row["lock_version"] != expected_version:
                raise FactMemoryError("concurrent_decision")
            target_state = {"approve": "APPROVED", "reject": "REJECTED", "revoke": "REVOKED"}[action]
            required_state = "APPROVED" if action == "revoke" else "PENDING"
            if row["status"] != required_state:
                raise FactMemoryError("invalid_transition")
            if action == "approve":
                if kind == "candidate":
                    if snapshot is None or not row["source_valid"] or row["source_token"] != snapshot.source_token:
                        raise FactMemoryError("source_invalid")
                    current = await repo.snapshot(target_id)
                    if current.record.candidate != snapshot.record.candidate or current.source != snapshot.source:
                        raise FactMemoryError("source_invalid")
                else:
                    for endpoint in (row["left_id"], row["right_id"]):
                        fact = await repo.candidate_row(endpoint)
                        if fact is None or fact["status"] != "APPROVED" or not fact["source_valid"]:
                            raise FactMemoryError("source_invalid")
            receipt = await repo.audit(target_id=target_id, kind=kind, action=action, reviewer_id=reviewer_id,
                                       request_id=request_id, expected_version=expected_version)
            table = "kulai_fact_candidates" if kind == "candidate" else "kulai_fact_relations"
            await repo.command(f"UPDATE public.{table} SET status=:status,lock_version=lock_version+1 WHERE id=:id",
                               {"id": target_id, "status": target_state})
            if kind == "candidate":
                if action == "approve":
                    await repo.command("INSERT INTO public.kulai_fact_approvals(candidate_id,approved_at,reviewer_id) VALUES(:id,:at,:actor)",
                                       {"id": target_id, "at": receipt.decided_at, "actor": reviewer_id})
                elif action == "revoke":
                    await repo.command("UPDATE public.kulai_fact_approvals SET revoked_at=:at,revocation_reason='reviewer' WHERE candidate_id=:id",
                                       {"id": target_id, "at": receipt.decided_at})
        return receipt

    async def list_decisions(self, target_id, *, limit):
        async with self._tx(read_only=True) as repo:
            rows = await repo.rows("SELECT * FROM public.kulai_fact_decisions WHERE target_id=:id ORDER BY decided_at,request_id LIMIT :limit",
                                   {"id": target_id, "limit": limit})
            return tuple(DecisionReceipt(**dict(row)) for row in rows)

    async def list_relations(self, *, status, limit, after_id):
        left_valid = f"EXISTS(SELECT 1 {CANDIDATE_FROM} WHERE c.id=r.left_id AND ({VALID_SOURCE})" + (" AND c.status='APPROVED'" if status == "APPROVED" else "") + ")"
        right_valid = left_valid.replace("r.left_id", "r.right_id")
        async with self._tx(read_only=True) as repo:
            rows = await repo.rows(f"SELECT r.*,({left_valid} AND {right_valid}) AS source_valid FROM public.kulai_fact_relations r "
                f"WHERE r.status=:status AND ({left_valid} AND {right_valid})" +
                (" AND r.id>:after" if after_id else "") + " ORDER BY r.id LIMIT :limit",
                {"status": status, "limit": limit, "after": after_id})
            return tuple(StoredRelation(id=r["id"], left_id=r["left_id"], right_id=r["right_id"], relation=r["relation"],
                status=r["status"], lock_version=r["lock_version"], source_valid=r["source_valid"],
                suggestion=RelationSuggestion.model_validate_json(json.dumps(r["payload"]))) for r in rows)


@dataclass(frozen=True)
class LocalFactSession:
    service: FactMemoryService
    principal: object = field(repr=False)
    storage: OwnedFactStorage = field(repr=False)

    async def analyze_and_stage(self, runtime, memory_ids, *, speaker_id=None, top_k=5):
        if (not isinstance(memory_ids, tuple) or not 1 <= len(memory_ids) <= 20
            or any(not isinstance(value, UUID) for value in memory_ids) or len(set(memory_ids)) != len(memory_ids)):
            raise FactMemoryError("invalid_input")
        documents = tuple([await self.storage.memory_document(value, speaker_id=speaker_id) for value in memory_ids])
        # Every document read has released its transaction before the existing runtime calls Qwen/BGE.
        preview = await runtime.preview(documents[0], documents[1:], top_k=top_k)
        batch = StageBatch(sources=tuple(StageSource(source=source, extraction=extraction)
                          for source, extraction in zip(documents, preview.extractions)), relations=preview.relations)
        return preview, await self.service.stage(batch)


@asynccontextmanager
async def open_owned_fact_memory(owned, *, config, intelligence):
    """Authority derives from trusted local launch + marker capability, never a reviewer string."""
    try:
        if get_settings().app_env.lower() not in SAFE_LOCAL_APP_ENVS or not database_host_is_loopback(config):
            raise FactMemoryError("owned_database_required")
        await require_owned_database(owned, config=config)
        target = database_config_for_database(owned.name, config=config)
    except Exception:
        raise FactMemoryError("owned_database_required") from None
    engine = create_async_engine(target.async_url, echo=False, hide_parameters=True, pool_pre_ping=True)
    try:
        storage, authority = OwnedFactStorage(engine, owned), _LocalAuthority()
        async with storage._tx(read_only=True):
            pass
        yield LocalFactSession(FactMemoryService(storage=storage, authority=authority, intelligence=intelligence), authority.principal, storage)
    finally:
        await engine.dispose()
