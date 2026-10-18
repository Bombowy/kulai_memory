"""Extraction, staging and human review are distinct application operations."""

from uuid import UUID

from ..intelligence import MemoryIntelligenceService
from ..intelligence.service import _evidence, _scope
from .models import FactMemoryError, ReviewSnapshot, StageBatch
from .ports import FactStorage, ReviewerAuthority


class FactMemoryService:
    def __init__(self, *, storage: FactStorage, authority: ReviewerAuthority,
                 intelligence: MemoryIntelligenceService):
        self._storage, self._authority, self._intelligence = storage, authority, intelligence

    @staticmethod
    def _id(value):
        if not isinstance(value, UUID):
            raise FactMemoryError("invalid_input")

    @staticmethod
    def _page(limit, after_id):
        if type(limit) is not int or not 1 <= limit <= 100 or (after_id is not None and not isinstance(after_id, UUID)):
            raise FactMemoryError("invalid_input")

    async def stage(self, batch: StageBatch):
        if not isinstance(batch, StageBatch):
            raise FactMemoryError("invalid_input")
        labels, candidate_ids = set(), set()
        try:
            for item in batch.sources:
                source = item.source
                if source.source_id in labels or item.extraction.source_id != source.source_id:
                    raise ValueError
                labels.add(source.source_id)
                local_ids = set()
                if len(item.extraction.candidates) > 20:
                    raise ValueError
                for candidate in item.extraction.candidates:
                    p = candidate.provenance
                    if (candidate.candidate_id in candidate_ids or p.source_id != source.source_id
                        or p.source_sha256 != source.content_sha256 or p.memory_id != source.memory_id
                        or p.revision != source.revision or p.recorded_at != source.created_at
                        or p.evidence != _evidence(source, candidate.fact)
                        or candidate.subject_scope != _scope(source, candidate.fact)):
                        raise ValueError
                    candidate_ids.add(candidate.candidate_id)
                    local_ids.add(candidate.candidate_id)
                bindings = [binding.preview_candidate_id for binding in item.revisions]
                fact_ids = [binding.fact_id for binding in item.revisions]
                if len(set(bindings)) != len(bindings) or len(set(fact_ids)) != len(fact_ids) or not set(bindings) <= local_ids:
                    raise ValueError
            for relation in batch.relations:
                if relation.left_id == relation.right_id or not {relation.left_id, relation.right_id} <= candidate_ids:
                    raise ValueError
                if relation.replacement_candidate_id is not None and relation.replacement_candidate_id not in {relation.left_id, relation.right_id}:
                    raise ValueError
        except Exception:
            raise FactMemoryError("invalid_provenance") from None
        return await self._storage.stage(batch)

    async def get_candidate(self, candidate_id: UUID):
        self._id(candidate_id)
        return await self._storage.get_candidate(candidate_id)

    async def list_pending(self, *, limit=50, after_id=None):
        self._page(limit, after_id)
        return await self._storage.list_candidates(status="PENDING", limit=limit, after_id=after_id)

    async def list_approved(self, *, limit=50, after_id=None):
        self._page(limit, after_id)
        return await self._storage.list_candidates(status="APPROVED", limit=limit, after_id=after_id)

    async def _decide(self, target_id, principal, request_id, expected_version, action, kind):
        self._id(target_id); self._id(request_id)
        if type(expected_version) is not int or expected_version < 1:
            raise FactMemoryError("invalid_input")
        reviewer_id = self._authority.require(principal)
        previous = await self._storage.receipt(request_id)
        if previous is not None:
            if (previous.target_id, previous.target_kind, previous.action, previous.reviewer_id, previous.expected_version) != (
                target_id, kind, action, reviewer_id, expected_version,
            ):
                raise FactMemoryError("idempotency_conflict")
            return previous  # Historical receipt only; never restores candidate state or private content.
        try:
            snapshot: ReviewSnapshot | None = None
            if action == "approve" and kind == "candidate":
                snapshot = await self._storage.review_snapshot(target_id)
                record = snapshot.record
                if record.lock_version != expected_version:
                    raise FactMemoryError("concurrent_decision")
                if record.status != "PENDING" or not record.source_valid or record.candidate is None:
                    raise FactMemoryError("invalid_transition")
                try:
                    verified = await self._intelligence.verify_candidates(snapshot.source, (record.candidate,))
                except Exception:
                    raise FactMemoryError("semantic_rejected") from None
                if (len(verified.verdicts) != 1 or verified.verdicts[0].candidate_id != record.candidate.candidate_id
                    or verified.verdicts[0].verdict != "supported" or verified.verdicts[0].reason != "supported"):
                    raise FactMemoryError("semantic_rejected")
            return await self._storage.decide(
                target_id=target_id, kind=kind, action=action, reviewer_id=reviewer_id,
                request_id=request_id, expected_version=expected_version, snapshot=snapshot,
            )
        except FactMemoryError:
            # Another caller may have committed this SAME request after our first receipt read.
            replay = await self._storage.receipt(request_id)
            if replay is not None:
                if (replay.target_id, replay.target_kind, replay.action, replay.reviewer_id, replay.expected_version) != (
                    target_id, kind, action, reviewer_id, expected_version,
                ):
                    raise FactMemoryError("idempotency_conflict") from None
                return replay
            raise

    async def approve(self, candidate_id, *, principal, request_id, expected_version):
        return await self._decide(candidate_id, principal, request_id, expected_version, "approve", "candidate")

    async def reject(self, candidate_id, *, principal, request_id, expected_version):
        return await self._decide(candidate_id, principal, request_id, expected_version, "reject", "candidate")

    async def revoke_approval(self, candidate_id, *, principal, request_id, expected_version):
        return await self._decide(candidate_id, principal, request_id, expected_version, "revoke", "candidate")

    async def approve_relation(self, relation_id, *, principal, request_id, expected_version):
        return await self._decide(relation_id, principal, request_id, expected_version, "approve", "relation")

    async def reject_relation(self, relation_id, *, principal, request_id, expected_version):
        return await self._decide(relation_id, principal, request_id, expected_version, "reject", "relation")

    async def revoke_relation(self, relation_id, *, principal, request_id, expected_version):
        return await self._decide(relation_id, principal, request_id, expected_version, "revoke", "relation")

    async def list_relations(self, *, status="PENDING", limit=50, after_id=None):
        self._page(limit, after_id)
        if status not in {"PENDING", "APPROVED", "REJECTED", "REVOKED"}:
            raise FactMemoryError("invalid_input")
        return await self._storage.list_relations(status=status, limit=limit, after_id=after_id)

    async def list_decisions(self, target_id, *, limit=50):
        self._id(target_id); self._page(limit, None)
        return await self._storage.list_decisions(target_id, limit=limit)
