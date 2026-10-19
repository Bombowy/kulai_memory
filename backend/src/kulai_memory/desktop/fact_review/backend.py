"""Owned-only desktop read model and genuine 5A.2 review commands."""

from uuid import UUID
from sqlalchemy import text

from ...application.fact_memory import FactMemoryError
from ...application.fact_memory.models import DecisionReceipt, StoredRelation
from ...application.intelligence.models import RelationSuggestion
from ...fact_persistence.repository import CANDIDATE_FROM, VALID_SOURCE
from .models import Detail, Intent, Outcome, Query, Row, View


STATUSES = {"PENDING", "APPROVED", "REJECTED", "REVOKED"}
TYPES = {"fact", "preference", "plan", "hypothesis"}
RELATIONS = {"exact_duplicate", "semantic_duplicate", "related", "possible_conflict", "possible_supersession"}


class FactReviewBackend:
    def __init__(self, local, *, database_name):
        self._local = local
        self.database_name = database_name

    async def read(self, query: Query):
        if (not isinstance(query, Query) or query.mode not in {"facts", "relations"}
            or query.status not in STATUSES or query.information_type not in TYPES | {None}
            or query.relation not in RELATIONS | {None} or type(query.limit) is not int or not 1 <= query.limit <= 50
            or any(value is not None and not isinstance(value, UUID) for value in (query.after, query.selected))):
            raise FactMemoryError("invalid_input")
        # One consistent read-only snapshot; marker verification is inherited
        # from the owned adapter. No raw URL or unchecked session is accepted.
        async with self._local.storage._tx(read_only=True) as repo:
            pending = await repo.db.scalar(text(
                "SELECT count(*) " + CANDIDATE_FROM + f" WHERE c.status='PENDING' AND ({VALID_SOURCE})"))
            params = {"status": query.status, "type": query.information_type, "kind": query.relation,
                      "limit": query.limit + 1, "after": query.after}
            if query.mode == "facts":
                base = CANDIDATE_FROM + f" WHERE c.status=:status AND ({VALID_SOURCE})"
                if query.information_type:
                    base += " AND c.information_type=:type"
                total = (await repo.one("SELECT count(*) AS n " + base, params))["n"]
                rows = await repo.rows("SELECT c.*," + f"({VALID_SOURCE}) AS source_valid " + base +
                    (" AND c.id>:after" if query.after else "") + " ORDER BY c.id LIMIT :limit", params)
                summaries = tuple(self._fact_row(repo.record(row)) for row in rows[:query.limit])
            else:
                valid = self._valid_relation(query.status)
                base = ("FROM public.kulai_fact_relations r "
                    "JOIN public.kulai_fact_candidates lf ON lf.id=r.left_id "
                    "JOIN public.kulai_fact_candidates rf ON rf.id=r.right_id "
                    f"WHERE r.status=:status AND ({valid})")
                if query.relation:
                    base += " AND r.relation=:kind"
                total = (await repo.one("SELECT count(*) AS n " + base, params))["n"]
                rows = await repo.rows("SELECT r.*,lf.payload->'fact'->>'text' AS left_claim,rf.payload->'fact'->>'text' AS right_claim " + base +
                    (" AND r.id>:after" if query.after else "") + " ORDER BY r.id LIMIT :limit", params)
                summaries = tuple(Row(r["id"], r["status"], r["lock_version"],
                    (r["relation"], r["status"], r["left_claim"], r["right_claim"])) for r in rows[:query.limit])
            detail = None
            if query.selected:
                detail = await self._detail(repo, query.selected, query.mode)
            return View(summaries, pending, total,
                        summaries[-1].id if len(rows) > query.limit else None, detail,
                        query.selected is not None and detail is None)

    @staticmethod
    def _valid_relation(status):
        approved = " AND c.status='APPROVED'" if status == "APPROVED" else ""
        return " AND ".join(f"EXISTS(SELECT 1 {CANDIDATE_FROM} WHERE c.id=r.{side}_id AND ({VALID_SOURCE}){approved})"
                            for side in ("left", "right"))

    @staticmethod
    def _fact_row(record):
        fact = record.candidate.fact
        return Row(record.id, record.status, record.lock_version,
                   (fact.text, fact.information_type, fact.subject, fact.project_context or "—", record.status))

    async def _detail(self, repo, target, mode):
        relation = None
        versions, related = (), ()
        try:
            if mode == "facts":
                snapshots = (await repo.snapshot(target),)
                record = snapshots[0].record
                status, lock_version = record.status, record.lock_version
                versions = tuple((r["version"], r["status"] + (" / NIEAKTUALNE" if not r["source_valid"] else "")) for r in await repo.rows(
                    f"SELECT c.version,c.status,({VALID_SOURCE}) AS source_valid " + CANDIDATE_FROM +
                    " WHERE c.fact_id=:id ORDER BY c.version DESC LIMIT 100",
                    {"id": record.fact_id}))
                related = tuple((r["id"], r["relation"], r["status"]) for r in await repo.rows(
                    "SELECT r.id,r.relation,r.status FROM public.kulai_fact_relations r WHERE "
                    "(r.left_id=:id OR r.right_id=:id) AND (" + self._valid_relation("PENDING") + ") ORDER BY r.id LIMIT 50", {"id": target}))
            else:
                r = await repo.one("SELECT * FROM public.kulai_fact_relations WHERE id=:id", {"id": target})
                if r is None:
                    return None
                snapshots = (await repo.snapshot(r["left_id"]), await repo.snapshot(r["right_id"]))
                if r["status"] == "APPROVED" and any(s.record.status != "APPROVED" for s in snapshots):
                    return None
                import json
                relation = StoredRelation(id=r["id"], left_id=r["left_id"], right_id=r["right_id"], relation=r["relation"],
                    status=r["status"], lock_version=r["lock_version"], source_valid=True,
                    suggestion=RelationSuggestion.model_validate_json(json.dumps(r["payload"])))
                status, lock_version = relation.status, relation.lock_version
            decisions = tuple(DecisionReceipt(**dict(r)) for r in await repo.rows(
                "SELECT * FROM public.kulai_fact_decisions WHERE target_id=:id ORDER BY decided_at DESC,request_id LIMIT 100", {"id": target}))
            return Detail(target, status, lock_version, snapshots, decisions, versions, related, relation)
        except FactMemoryError as error:
            if error.code in {"facts.source_invalid", "facts.not_found"}:
                return None
            raise

    async def decide(self, intent: Intent):
        if (not isinstance(intent, Intent) or intent.kind not in {"candidate", "relation"}
            or intent.action not in {"approve", "reject", "revoke"}):
            raise FactMemoryError("invalid_input")
        service = self._local.service
        name = {("candidate", "approve"): "approve", ("candidate", "reject"): "reject",
                ("candidate", "revoke"): "revoke_approval", ("relation", "approve"): "approve_relation",
                ("relation", "reject"): "reject_relation", ("relation", "revoke"): "revoke_relation"}[(intent.kind, intent.action)]
        try:
            receipt = await getattr(service, name)(intent.target, principal=self._local.principal,
                request_id=intent.request_id, expected_version=intent.expected_version)
            return Outcome("recorded", receipt)
        except Exception as error:
            if isinstance(error, FactMemoryError) and error.code != "facts.operation_failed":
                return Outcome("denied", error_code=error.code)
            # An unknown commit outcome is NEVER retried with a new UUID.
            try:
                outcome = await self.resolve(intent)
                return outcome if outcome.state == "recorded" else Outcome("uncertain")
            except Exception:
                return Outcome("uncertain")

    async def resolve(self, intent):
        reviewer = self._local.service._authority.require(self._local.principal)
        receipt = await self._local.storage.receipt(intent.request_id)
        if receipt is not None:
            if (receipt.target_id, receipt.target_kind, receipt.action, receipt.expected_version, receipt.reviewer_id) != (
                intent.target, intent.kind, intent.action, intent.expected_version, reviewer):
                raise FactMemoryError("idempotency_conflict")
        return Outcome("recorded" if receipt else "absent", receipt)
