"""Private by default, strict, portable Fact-RAG contracts."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from ..intelligence.models import StrictModel, EvidenceSpan, InformationType, EntityKind


class FactRagError(RuntimeError):
    def __init__(self, code="operation_failed"):
        allowed = {"invalid_input", "read_failed", "source_invalid", "invalid_provenance",
                   "embedding_failed", "generation_failed", "invalid_citations", "ungrounded_answer"}
        self.code = "fact_rag." + (code if code in allowed else "operation_failed")
        super().__init__(self.code)


class FactReference(StrictModel):
    fact_id: UUID
    fact_version: int = Field(ge=1)
    candidate_id: UUID
    memory_id: UUID
    revision: int = Field(ge=1)
    incarnation: UUID
    generation: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovedFact(StrictModel):
    reference: FactReference
    lock_version: int = Field(ge=1)
    text: str = Field(repr=False)
    information_type: InformationType
    subject: str = Field(repr=False)
    subject_kind: EntityKind
    subject_scope: str = Field(repr=False)
    project_context: str | None = Field(repr=False)
    temporal_context: str | None = Field(repr=False)
    polarity: str
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1, repr=False)


class FactRelation(StrictModel):
    id: UUID
    left_id: UUID
    right_id: UUID
    kind: Literal["exact_duplicate", "semantic_duplicate", "related", "possible_conflict", "possible_supersession"]
    status: Literal["PENDING", "APPROVED"]
    lock_version: int = Field(ge=1)


class ApprovedCollection(StrictModel):
    facts: tuple[ApprovedFact, ...] = Field(repr=False)
    relations: tuple[FactRelation, ...]
    more_candidates: bool = False
    more_relations: bool = False


class FactHit(StrictModel):
    fact: ApprovedFact = Field(repr=False)
    score: float = Field(allow_inf_nan=False)


class FactRetrieval(StrictModel):
    hits: tuple[FactHit, ...] = Field(repr=False)
    relations: tuple[FactRelation, ...]
    scanned_candidates: int
    partial_reasons: tuple[str, ...]


class GroundedDraft(StrictModel):
    answer: str = Field(min_length=1, max_length=4000, repr=False)
    sufficient_context: bool
    # Local tokens resolve to immutable, server-owned FactReference objects.
    # The model has no authority to supply/overwrite Memory IDs or epochs.
    citations: list[str] = Field(max_length=20)


class GroundingVerdict(StrictModel):
    supported: bool


class FactAnswer(StrictModel):
    answer: str = Field(repr=False)
    sufficient_context: bool
    citations: tuple[ApprovedFact, ...] = Field(repr=False)
    relations: tuple[FactRelation, ...]
    partial_coverage: bool
    partial_reasons: tuple[str, ...]
    scanned_candidates: int
    context_chars: int
    dry_run: Literal[True] = True
