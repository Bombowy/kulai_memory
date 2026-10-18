"""SQL operations on a caller-owned connection. No commit, rollback or engine creation."""

import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from ..application.fact_memory.models import CandidateRecord, DecisionReceipt, FactMemoryError, ReviewSnapshot
from ..application.intelligence import FactCandidate, SourceDocument

MEMORY_SOURCE_NAMESPACE = UUID("93044fea-8abc-485b-a371-0f25241dbb89")

VALID_SOURCE = """v.invalidated_at IS NULL AND (
 (s.memory_id IS NOT NULL AND m.id IS NOT NULL AND m.archived_at IS NULL
  AND m.revision=v.revision AND m.xmin::text=v.source_token
  AND encode(sha256(convert_to(m.content,'UTF8')),'hex')=v.sha256
  AND NOT EXISTS(SELECT 1 FROM public.memory_ingestion_tombstones t WHERE t.memory_id=m.id))
 OR (s.memory_id IS NULL AND s.local_revision=v.revision AND s.local_token::text=v.source_token
  AND encode(sha256(convert_to(s.local_text,'UTF8')),'hex')=v.sha256))"""
CANDIDATE_FROM = """FROM public.kulai_fact_candidates c
 JOIN public.kulai_fact_source_versions v ON v.id=c.source_version_id
 JOIN public.kulai_fact_sources s ON s.id=v.source_id
 LEFT JOIN public.memories m ON m.id=s.memory_id"""


def memory_source_id(memory_id):
    return uuid5(MEMORY_SOURCE_NAMESPACE, str(memory_id))


def private_json(model):
    return model.model_dump_json()


class PostgresFactRepository:
    def __init__(self, connection):
        self.db = connection

    async def one(self, sql, params=None):
        return (await self.db.execute(text(sql), params or {})).mappings().one_or_none()

    async def rows(self, sql, params=None):
        return (await self.db.execute(text(sql), params or {})).mappings().all()

    async def command(self, sql, params=None):
        return await self.db.execute(text(sql), params or {})

    async def retired(self, source_id):
        return bool(await self.db.scalar(text("""SELECT EXISTS(SELECT 1 FROM public.kulai_fact_decisions
            WHERE target_id=:id AND target_kind='source' AND action='source_deleted')"""), {"id": source_id}))

    async def memory(self, memory_id, *, lock=False):
        return await self.one("""SELECT id,content,revision,archived_at,created_at,xmin::text AS token
            FROM public.memories WHERE id=:id""" + (" FOR UPDATE" if lock else ""), {"id": memory_id})

    async def source(self, source_id, *, lock=False):
        return await self.one("SELECT * FROM public.kulai_fact_sources WHERE id=:id" + (" FOR UPDATE" if lock else ""), {"id": source_id})

    async def stamp(self, document, *, lock=False):
        if document.memory_id is not None:
            current = await self.memory(document.memory_id, lock=lock)
            if (current is None or current["archived_at"] is not None
                or current["revision"] != document.revision or current["content"] != document.text
                or document.created_at != current["created_at"]):
                raise FactMemoryError("source_invalid")
            retired = await self.db.scalar(text("SELECT EXISTS(SELECT 1 FROM public.memory_ingestion_tombstones WHERE memory_id=:id)"), {"id": document.memory_id})
            if retired:
                raise FactMemoryError("source_invalid")
            return memory_source_id(document.memory_id), current["revision"], current["token"]
        try:
            source_id = UUID(document.source_id)
        except ValueError:
            raise FactMemoryError("source_invalid") from None
        current = await self.source(source_id, lock=lock)
        if current is None or current["memory_id"] is not None or current["local_text"] != document.text:
            raise FactMemoryError("source_invalid")
        return source_id, current["local_revision"], str(current["local_token"])

    async def invalidate(self, source_id, *, current_token=None, reason="source_changed"):
        params = {"source": source_id, "token": current_token, "reason": reason}
        versions = await self.rows("""UPDATE public.kulai_fact_source_versions SET
            invalidated_at=clock_timestamp(),invalidation_reason=:reason
            WHERE source_id=:source AND invalidated_at IS NULL
            AND (CAST(:token AS text) IS NULL OR source_token<>CAST(:token AS text)) RETURNING id""", params)
        for version in versions:
            await self.command("""INSERT INTO public.kulai_fact_decisions
                (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
                VALUES(gen_random_uuid(),:id,'source_version','invalidate','system:source-invalidated',0,0)""", {"id": version["id"]})
            changed = await self.rows("""UPDATE public.kulai_fact_candidates SET status='REVOKED',lock_version=lock_version+1
                WHERE source_version_id=:id AND status='APPROVED' RETURNING id,lock_version""", {"id": version["id"]})
            for candidate in changed:
                await self.command("""UPDATE public.kulai_fact_approvals SET revoked_at=clock_timestamp(),revocation_reason=:reason
                    WHERE candidate_id=:id AND revoked_at IS NULL""", {"id": candidate["id"], "reason": reason})
                await self.command("""INSERT INTO public.kulai_fact_decisions
                    (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
                    VALUES(gen_random_uuid(),:id,'candidate','invalidate','system:source-invalidated',:before,:after)""",
                    {"id": candidate["id"], "before": candidate["lock_version"] - 1, "after": candidate["lock_version"]})

    async def candidate_row(self, candidate_id):
        return await self.one("SELECT c.*,v.source_id,v.document_meta,v.source_token,v.sha256," +
                              " CASE WHEN s.memory_id IS NULL THEN s.local_text ELSE m.content END AS live_text," +
                              f" ({VALID_SOURCE}) AS source_valid " + CANDIDATE_FROM + " WHERE c.id=:id", {"id": candidate_id})

    @staticmethod
    def record(row):
        return CandidateRecord(
            id=row["id"], fact_id=row["fact_id"], version=row["version"], source_version_id=row["source_version_id"],
            status=row["status"], lock_version=row["lock_version"], source_valid=bool(row["source_valid"]),
            candidate=FactCandidate.model_validate_json(json.dumps(row["payload"])) if row["source_valid"] else None,
            created_at=row["created_at"],
        )

    async def snapshot(self, candidate_id):
        row = await self.candidate_row(candidate_id)
        if row is None:
            raise FactMemoryError("not_found")
        if not row["source_valid"]:
            raise FactMemoryError("source_invalid")
        source = SourceDocument.model_validate_json(json.dumps({**row["document_meta"], "text": row["live_text"]}))
        return ReviewSnapshot(record=self.record(row), source=source, source_token=row["source_token"])

    async def receipt(self, request_id):
        row = await self.one("SELECT * FROM public.kulai_fact_decisions WHERE request_id=:id", {"id": request_id})
        return DecisionReceipt(**dict(row)) if row is not None else None

    async def audit(self, *, target_id, kind, action, reviewer_id, request_id, expected_version):
        row = await self.one("""INSERT INTO public.kulai_fact_decisions
            (request_id,target_id,target_kind,action,reviewer_id,expected_version,resulting_version)
            VALUES(:request,:target,:kind,:action,:actor,:expected,:result) RETURNING *""",
            {"request": request_id, "target": target_id, "kind": kind, "action": action,
             "actor": reviewer_id, "expected": expected_version, "result": expected_version + 1})
        return DecisionReceipt(**dict(row))

    async def stage_source(self, item):
        source = item.source
        source_id, revision, token = await self.stamp(source, lock=True)
        if await self.retired(source_id):
            raise FactMemoryError("source_invalid")
        if source.memory_id is not None:
            await self.command("""INSERT INTO public.kulai_fact_sources(id,memory_id) VALUES(:id,:memory)
                ON CONFLICT(id) DO NOTHING""", {"id": source_id, "memory": source.memory_id})
        parent = await self.source(source_id, lock=True)
        if parent is None or parent["memory_id"] != source.memory_id:
            raise FactMemoryError("source_invalid")
        previous = await self.one("""SELECT id FROM public.kulai_fact_source_versions
            WHERE source_id=:source AND revision=:revision AND source_token=:token""",
            {"source": source_id, "revision": revision, "token": token})
        if previous is not None:
            rows = await self.rows("SELECT * FROM public.kulai_fact_candidates WHERE source_version_id=:id ORDER BY ordinal", {"id": previous["id"]})
            # First complete analysis wins; only exactly matching claims may map new relation references.
            by_payload = {json.dumps({"fact": row["payload"]["fact"], "scope": row["payload"]["subject_scope"]},
                                    sort_keys=True, ensure_ascii=True): row["id"] for row in rows}
            mapping = {c.candidate_id: by_payload[key] for c in item.extraction.candidates
                       if (key := json.dumps({"fact": c.fact.model_dump(mode="json"), "scope": c.subject_scope},
                                             sort_keys=True, ensure_ascii=True)) in by_payload}
            return [row["id"] for row in rows], mapping, True
        await self.invalidate(source_id, current_token=token)
        version_id = uuid4()
        number = await self.db.scalar(text("SELECT COALESCE(max(number),0)+1 FROM public.kulai_fact_source_versions WHERE source_id=:id"), {"id": source_id})
        from ..application.intelligence.prompts import EXTRACT_PROMPT, VERIFY_PROMPT, RELATE_PROMPT
        import hashlib
        pipeline = "qwen3.5:9b/" + hashlib.sha256((EXTRACT_PROMPT + VERIFY_PROMPT + RELATE_PROMPT).encode()).hexdigest()
        await self.command("""INSERT INTO public.kulai_fact_source_versions
            (id,source_id,number,revision,sha256,source_token,document_meta,pipeline)
            VALUES(:id,:source,:number,:revision,:hash,:token,CAST(:meta AS jsonb),:pipeline)""",
            {"id": version_id, "source": source_id, "number": number, "revision": revision,
             "hash": source.content_sha256, "token": token,
             "meta": json.dumps(source.model_dump(mode="json", exclude={"text"})), "pipeline": pipeline})
        bindings = {b.preview_candidate_id: b.fact_id for b in item.revisions}
        ids, mapping = [], {}
        for ordinal, candidate in enumerate(item.extraction.candidates, 1):
            fact_id = bindings.get(candidate.candidate_id)
            version = 1
            if fact_id is not None:
                identity = await self.one("SELECT source_id FROM public.kulai_fact_identities WHERE id=:id FOR UPDATE", {"id": fact_id})
                if identity is None or identity["source_id"] != source_id:
                    raise FactMemoryError("invalid_input")
                version = await self.db.scalar(text("SELECT COALESCE(max(version),0)+1 FROM public.kulai_fact_candidates WHERE fact_id=:id"), {"id": fact_id})
            else:
                fact_id = uuid4()
                await self.command("INSERT INTO public.kulai_fact_identities(id,source_id) VALUES(:id,:source)", {"id": fact_id, "source": source_id})
            candidate_id = uuid4()
            await self.command("""INSERT INTO public.kulai_fact_candidates
                (id,fact_id,source_version_id,version,ordinal,information_type,payload)
                VALUES(:id,:fact,:source,:version,:ordinal,:type,CAST(:payload AS jsonb))""",
                {"id": candidate_id, "fact": fact_id, "source": version_id, "version": version,
                 "ordinal": ordinal, "type": candidate.fact.information_type, "payload": private_json(candidate)})
            ids.append(candidate_id); mapping[candidate.candidate_id] = candidate_id
        return ids, mapping, False
