"""Portable contracts for staged fact versions and authenticated review decisions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from ..intelligence.models import (
    ExtractionResult, FactCandidate, RelationSuggestion, SourceDocument, StrictModel,
)

ReviewStatus = Literal["PENDING", "APPROVED", "REJECTED", "REVOKED"]
ReviewAction = Literal["approve", "reject", "revoke"]


class FactMemoryError(RuntimeError):
    def __init__(self, code: str = "operation_failed"):
        allowed = {"operation_failed", "forbidden", "owned_database_required", "schema_required",
                   "invalid_input", "invalid_provenance", "source_invalid", "not_found",
                   "concurrent_decision", "invalid_transition", "idempotency_conflict", "semantic_rejected"}
        self.code = "facts." + (code if code in allowed else "operation_failed")
        super().__init__(self.code)


class RevisionBinding(StrictModel):
    preview_candidate_id: str
    fact_id: UUID


class StageSource(StrictModel):
    source: SourceDocument = Field(repr=False)
    extraction: ExtractionResult = Field(repr=False)
    revisions: tuple[RevisionBinding, ...] = ()


class StageBatch(StrictModel):
    sources: tuple[StageSource, ...] = Field(min_length=1, max_length=20, repr=False)
    relations: tuple[RelationSuggestion, ...] = Field(default=(), max_length=400, repr=False)


class StagedBatch(StrictModel):
    candidate_ids: tuple[UUID, ...]
    relation_ids: tuple[UUID, ...]
    reused_sources: int
    skipped_relations: int


class CandidateRecord(StrictModel):
    id: UUID
    fact_id: UUID
    version: int
    source_version_id: UUID
    status: ReviewStatus
    lock_version: int
    source_valid: bool
    candidate: FactCandidate | None = Field(repr=False)
    created_at: datetime


class ReviewSnapshot(StrictModel):
    record: CandidateRecord = Field(repr=False)
    source: SourceDocument = Field(repr=False)
    source_token: str = Field(repr=False)


class DecisionReceipt(StrictModel):
    request_id: UUID
    target_id: UUID
    target_kind: Literal["candidate", "relation", "source", "source_version"]
    action: str
    reviewer_id: str
    decided_at: datetime
    expected_version: int
    resulting_version: int


class StoredRelation(StrictModel):
    id: UUID
    left_id: UUID
    right_id: UUID
    relation: str
    status: ReviewStatus
    lock_version: int
    source_valid: bool
    suggestion: RelationSuggestion | None = Field(repr=False)
