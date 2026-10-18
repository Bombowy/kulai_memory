"""Strict, transport-neutral preview contracts. No persistence capability."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SOURCE_CHARS = 12_000
MAX_CANDIDATES = 20
InformationType = Literal["fact", "preference", "plan", "hypothesis"]
EntityKind = Literal["person", "project", "technology", "other", "unknown"]
RelationKind = Literal[
    "exact_duplicate", "semantic_duplicate", "related", "possible_conflict",
    "possible_supersession", "unrelated", "uncertain",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class SourceDocument(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=MAX_SOURCE_CHARS, repr=False)
    memory_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1)
    created_at: datetime | None = None
    speaker_id: str | None = Field(default=None, min_length=1, max_length=128, repr=False)

    @model_validator(mode="after")
    def valid_origin(self):
        if (self.memory_id is None) != (self.revision is None):
            raise ValueError("Memory identity and revision must be supplied together.")
        if self.created_at is not None and self.created_at.utcoffset() is None:
            raise ValueError("Source timestamp must be timezone-aware.")
        return self

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class DraftFact(StrictModel):
    text: str = Field(min_length=1, max_length=1000, repr=False)
    information_type: InformationType
    subject: str = Field(min_length=1, max_length=200, repr=False,
                         description="WHO/WHAT performs the action, never its object. 'I use PostgreSQL' -> author.")
    subject_kind: EntityKind = Field(description="Kind of the SUBJECT: author/Anna=person; backend=project; never classify by the object.")
    subject_is_author: bool = Field(description="True only when subject is the first-person narrator; then subject_kind MUST be person.")
    project_context: str | None = Field(max_length=200, repr=False,
                                      description="Explicit project name supported by quotes, or null; no inferred attribution.")
    technologies: list[str] = Field(max_length=10, repr=False)
    polarity: Literal["positive", "negative", "unspecified"]
    temporal_context: str | None = Field(max_length=500, repr=False,
                                        description="Only an explicit WHEN phrase quoted verbatim, e.g. 'Since October'. null when no time phrase; NEVER the claim itself.")
    evidence_quotes: list[str] = Field(min_length=1, max_length=8, repr=False)


class ExtractionDraft(StrictModel):
    candidates: list[DraftFact] = Field(max_length=MAX_CANDIDATES, repr=False)


class EvidenceSpan(StrictModel):
    quote: str = Field(min_length=1, repr=False)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class Provenance(StrictModel):
    source_id: str
    source_sha256: str
    memory_id: UUID | None
    revision: int | None
    recorded_at: datetime | None
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1, repr=False)


class FactCandidate(StrictModel):
    candidate_id: str
    fact: DraftFact = Field(repr=False)
    subject_scope: str = Field(repr=False)
    provenance: Provenance = Field(repr=False)
    review_status: Literal["requires_review"] = "requires_review"


class VerificationVerdict(StrictModel):
    candidate_id: str
    verdict: Literal["supported", "unsupported", "uncertain"]
    reason: Literal[
        "supported", "not_entailed", "wrong_type", "wrong_subject", "wrong_time",
        "lost_negation", "instruction", "ambiguous", "insufficient_evidence",
    ]


class VerificationBatch(StrictModel):
    verdicts: list[VerificationVerdict] = Field(max_length=MAX_CANDIDATES)


class Rejection(StrictModel):
    candidate_id: str
    reason: str


class ExtractionResult(StrictModel):
    source_id: str
    candidates: tuple[FactCandidate, ...] = Field(repr=False)
    rejections: tuple[Rejection, ...]


class PairVerdict(StrictModel):
    pair_id: str
    relation: RelationKind
    rationale: str = Field(min_length=1, max_length=1500, repr=False)
    replacement_candidate_id: str | None


class PairBatch(StrictModel):
    verdicts: list[PairVerdict] = Field(max_length=20)


class RelationSuggestion(StrictModel):
    left_id: str
    right_id: str
    relation: RelationKind
    similarity: float | None = Field(default=None, allow_inf_nan=False)
    rationale: str = Field(repr=False)
    replacement_candidate_id: str | None = None
    validation_notes: tuple[str, ...] = ()
    review_status: Literal["requires_review"] = "requires_review"


class PreviewResult(StrictModel):
    extractions: tuple[ExtractionResult, ...] = Field(repr=False)
    relations: tuple[RelationSuggestion, ...] = Field(repr=False)
    comparison_scope: Literal["local_sources", "local_and_indexed_main"] = "local_sources"
    exhaustive: bool = False
    compared_pairs: int = Field(ge=0)
    available_pairs: int = Field(ge=0)
    top_k: int = Field(ge=1, le=20)
    dry_run: Literal[True] = True


class IntelligenceError(RuntimeError):
    """Allowlisted errors only; provider payloads and source text never escape."""

    def __init__(self, code: str = "operation_failed") -> None:
        allowed = {
            "operation_failed", "invalid_input", "invalid_configuration", "generation_failed",
            "invalid_verification", "invalid_relations", "embedding_failed", "retrieval_failed",
            "input_file_failed", "database_guard_failed",
        }
        self.code = "intelligence." + (code if code in allowed else "operation_failed")
        super().__init__(self.code)
