"""Read-only adapter, borrowing the already guarded owned storage. Facts v2 only."""

import json

from ..application.approved_facts.models import (
    ApprovedCollection, ApprovedFact, FactRagError, FactReference, FactRelation,
)
from ..application.intelligence import FactCandidate, SourceDocument
from ..application.intelligence.service import _evidence, _scope
from .owned import OwnedFactStorage
from .repository import CANDIDATE_FROM, VALID_SOURCE

SELECT = """SELECT c.*,v.source_id,v.source_token,v.sha256,v.document_meta,
    m.id AS live_memory_id,m.revision AS live_revision,m.created_at AS live_created,m.content AS live_text,
    e.incarnation,e.generation """ + CANDIDATE_FROM + """
    JOIN public.kulai_fact_approvals a ON a.candidate_id=c.id AND a.revoked_at IS NULL
    JOIN public.kulai_fact_identities i ON i.id=c.fact_id AND i.source_id=s.id
    WHERE c.status='APPROVED' AND s.memory_id IS NOT NULL AND (""" + VALID_SOURCE + ")"


def _fact(row):
    try:
        candidate = FactCandidate.model_validate_json(json.dumps(row["payload"]))
        source = SourceDocument.model_validate_json(json.dumps({**row["document_meta"], "text": row["live_text"]}))
        p = candidate.provenance
        if (source.memory_id != row["live_memory_id"] or source.revision != row["live_revision"]
            or source.created_at != row["live_created"] or p.source_id != source.source_id
            or p.source_sha256 != source.content_sha256 or row["sha256"] != source.content_sha256
            or p.memory_id != source.memory_id or p.revision != source.revision
            or p.recorded_at != source.created_at or p.evidence != _evidence(source, candidate.fact)
            or candidate.subject_scope != _scope(source, candidate.fact)
            or row["information_type"] != candidate.fact.information_type):
            raise ValueError
        f = candidate.fact
        return ApprovedFact(reference=FactReference(fact_id=row["fact_id"], fact_version=row["version"],
            candidate_id=row["id"], memory_id=source.memory_id, revision=source.revision,
            incarnation=row["incarnation"], generation=row["generation"], source_sha256=source.content_sha256),
            lock_version=row["lock_version"], text=f.text, information_type=f.information_type,
            subject=f.subject, subject_kind=f.subject_kind, subject_scope=candidate.subject_scope,
            project_context=f.project_context, temporal_context=f.temporal_context,
            polarity=f.polarity, evidence=p.evidence)
    except Exception:
        raise FactRagError("invalid_provenance") from None


class OwnedApprovedFactsReader:
    def __init__(self, storage: OwnedFactStorage):
        if not isinstance(storage, OwnedFactStorage):
            raise FactRagError("invalid_input")
        self.storage = storage

    async def _relations(self, repo, ids, relation_ids=None):
        return await repo.rows("""SELECT id,left_id,right_id,relation AS kind,status,lock_version
            FROM public.kulai_fact_relations WHERE status IN ('PENDING','APPROVED')
            AND left_id=ANY(CAST(:ids AS uuid[])) AND right_id=ANY(CAST(:ids AS uuid[]))""" +
            (" AND id=ANY(CAST(:relations AS uuid[]))" if relation_ids is not None else "") +
            " ORDER BY id LIMIT 101", {"ids": ids, "relations": relation_ids})

    async def read(self, *, limit=50, project=None, subject_scope=None):
        if (type(limit) is not int or not 1 <= limit <= 100
            or any(v is not None and (not isinstance(v, str) or not v.strip() or len(v)>n)
                   for v,n in ((project,200),(subject_scope,512)))):
            raise FactRagError("invalid_input")
        try:
            async with self.storage._tx(read_only=True) as repo:
                rows = await repo.rows(SELECT +
                    (" AND c.payload->'fact'->>'project_context'=:project" if project is not None else "") +
                    (" AND c.payload->>'subject_scope'=:scope" if subject_scope is not None else "") +
                    " ORDER BY c.id LIMIT :limit", {"project":project, "scope":subject_scope, "limit":limit+1})
                relations = await self._relations(repo, [r['id'] for r in rows[:limit]])
            # Validate the detached values from this consistent snapshot AFTER
            # rollback, outside the storage's write-domain error translation.
            facts = tuple(_fact(row) for row in rows[:limit])
            return ApprovedCollection(facts=facts,
                relations=tuple(FactRelation(**dict(r)) for r in relations[:100]),
                more_candidates=len(rows)>limit, more_relations=len(relations)>100)
        except FactRagError:
            raise
        except Exception:
            raise FactRagError("read_failed") from None

    async def revalidate(self, facts, relations):
        if (not isinstance(facts, tuple) or len(facts)>20 or not isinstance(relations, tuple)
            or len(relations)>100 or any(not isinstance(f, ApprovedFact) for f in facts)
            or any(not isinstance(r, FactRelation) for r in relations)):
            raise FactRagError("invalid_input")
        try:
            async with self.storage._tx(read_only=True) as repo:
                ids = [f.reference.candidate_id for f in facts]
                rows = await repo.rows(SELECT + " AND c.id=ANY(CAST(:ids AS uuid[])) ORDER BY c.id LIMIT 20", {"ids":ids})
                relation_rows = await self._relations(repo, ids, [r.id for r in relations])
            current = {f.reference.candidate_id:f for f in map(_fact, rows)}
            if len(current)!=len(facts) or any(current.get(f.reference.candidate_id)!=f for f in facts):
                raise FactRagError("source_invalid")
            linked = {r.id:r for r in (FactRelation(**dict(row)) for row in relation_rows)}
            if len(linked)!=len(relations) or any(linked.get(r.id)!=r for r in relations):
                raise FactRagError("source_invalid")
        except FactRagError:
            raise
        except Exception:
            raise FactRagError("read_failed") from None
