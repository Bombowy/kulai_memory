"""Source-grounded extraction and advisory relations, entirely in process."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Sequence
from typing import Literal

from kulai_embeddings import EmbeddingProvider, EmbeddingRequest, embed
from kulai_llm import LLMMessage, LLMProvider, LLMRequest, generate_structured
from pydantic import Field, create_model

from .models import (
    DraftFact, EvidenceSpan, ExtractionDraft, ExtractionResult, FactCandidate,
    IntelligenceError, PairBatch, PreviewResult, Provenance, Rejection,
    RelationSuggestion, SourceDocument, VerificationBatch,
)
from .prompts import EXTRACT_PROMPT, RELATE_PROMPT, VERIFY_PROMPT

LLM_TIMEOUT_SECONDS = 180.0
EMBEDDING_TIMEOUT_SECONDS = 60.0


def _extraction_schema(source: SourceDocument):
    """Constrain native JSON quote choices to actual source sentences, never repair output.

    The whole text is also available when multiple sentences supply necessary
    context. Whitespace and punctuation are retained exactly. Schema enum values
    are untrusted data, not system instructions.
    """
    segments = re.findall(r".+?(?:[.!?](?=\s|$)|\r?\n|$)", source.text, flags=re.DOTALL)
    choices = tuple(dict.fromkeys([part for part in segments if part.strip()] + [source.text]))
    quote_type = Literal[choices]
    fact_model = create_model("SourceFactDraft", __base__=DraftFact, evidence_quotes=(
        list[quote_type], Field(min_length=1, max_length=8, repr=False,
                               description="Choose EXACT enum values, including original spaces/punctuation; include all antecedent context."),
    ))
    return create_model("SourceExtractionDraft", __base__=ExtractionDraft,
                        candidates=(list[fact_model], Field(max_length=20, repr=False)))


def _key(value: str) -> str:
    return " ".join(value.casefold().split())


def _evidence(source: SourceDocument, fact: DraftFact) -> tuple[EvidenceSpan, ...]:
    spans: dict[tuple[int, int], EvidenceSpan] = {}
    for quote in fact.evidence_quotes:
        if not quote.strip():
            raise ValueError("invalid_quote")
        start = source.text.find(quote)
        if start < 0:
            raise ValueError("invalid_quote")
        while start >= 0:
            end = start + len(quote)
            spans[start, end] = EvidenceSpan(quote=quote, start=start, end=end)
            if len(spans) > 100:
                raise ValueError("excessive_quote_matches")
            start = source.text.find(quote, start + 1)
    joined = "\n".join(fact.evidence_quotes)
    if not fact.text.strip() or not fact.subject.strip():
        raise ValueError("empty_claim")
    if fact.temporal_context is not None and (
        not fact.temporal_context.strip() or fact.temporal_context not in joined
    ):
        raise ValueError("unsupported_time")
    if any(not term.strip() or _key(term) not in _key(joined) for term in fact.technologies):
        raise ValueError("unsupported_technology")
    if fact.project_context is not None and (
        not fact.project_context.strip() or _key(fact.project_context) not in _key(joined)
    ):
        raise ValueError("unsupported_project")
    if fact.subject_is_author and fact.subject_kind != "person":
        raise ValueError("invalid_author")
    return tuple(spans[key] for key in sorted(spans))


def _scope(source: SourceDocument, fact: DraftFact) -> str:
    if fact.subject_is_author:
        return "author:" + (source.speaker_id or "source:" + source.source_id)
    if fact.subject_kind == "unknown":
        return "unknown:" + source.source_id
    return "named:" + _key(fact.subject)


def _context_guard(left: FactCandidate, right: FactCandidate) -> str | None:
    a, b = left.fact, right.fact
    if a.project_context and b.project_context and _key(a.project_context) != _key(b.project_context):
        return "unrelated"
    if a.subject_kind == b.subject_kind == "person":
        if left.subject_scope != right.subject_scope:
            return "uncertain" if a.subject_is_author or b.subject_is_author else "unrelated"
    if a.subject_kind == "unknown" or b.subject_kind == "unknown":
        return "uncertain"
    if a.information_type != b.information_type:
        return "related"
    return None


def _exact(left: FactCandidate, right: FactCandidate) -> bool:
    fields = (
        "text", "information_type", "subject", "subject_kind", "subject_is_author",
        "project_context", "technologies", "polarity", "temporal_context",
    )
    return left.subject_scope == right.subject_scope and all(
        getattr(left.fact, field) == getattr(right.fact, field) for field in fields
    )


def _explicit_replacement(candidate: FactCandidate) -> bool:
    # A necessary, deliberately conservative lexical guard; not an entailment proof.
    # Different dates alone must not establish a replacement direction.
    evidence = " ".join(span.quote for span in candidate.provenance.evidence).casefold()
    return bool(re.search(
        r"\bzamiast\b|\bzast[ąę]p\w*|\binstead\s+of\b|\breplac\w*|"
        r"\bswitch\w*\s+from\b|\bmigrat\w*\s+from\b", evidence,
    ))


class MemoryIntelligenceService:
    """No database, filesystem, transport or write ports are accepted."""

    def __init__(self, *, llm_provider: LLMProvider, embedding_provider: EmbeddingProvider) -> None:
        self._llm = llm_provider
        self._bge = embedding_provider

    async def _structured(self, prompt: str, payload: dict, response_model):
        try:
            return await asyncio.wait_for(generate_structured(
                provider=self._llm,
                request=LLMRequest(messages=(
                    LLMMessage.system(prompt),
                    LLMMessage.user(json.dumps(payload, ensure_ascii=False)),
                ), temperature=0.0, max_output_tokens=8192),
                response_model=response_model,
            ), timeout=LLM_TIMEOUT_SECONDS)
        except Exception:
            raise IntelligenceError("generation_failed") from None

    async def extract(self, source: SourceDocument) -> ExtractionResult:
        if not isinstance(source, SourceDocument):
            raise IntelligenceError("invalid_input")
        if not source.text.strip():
            return ExtractionResult(source_id=source.source_id, candidates=(), rejections=())
        draft = await self._structured(EXTRACT_PROMPT, {"source": source.model_dump(mode="json")}, _extraction_schema(source))
        grounded: list[FactCandidate] = []
        rejections: list[Rejection] = []
        for index, fact in enumerate(draft.candidates, 1):
            candidate_id = f"{source.source_id}:{index}"
            try:
                evidence = _evidence(source, fact)
            except ValueError as exc:
                rejections.append(Rejection(candidate_id=candidate_id, reason=str(exc)))
                continue
            grounded.append(FactCandidate(
                candidate_id=candidate_id, fact=fact, subject_scope=_scope(source, fact),
                provenance=Provenance(
                    source_id=source.source_id, source_sha256=source.content_sha256,
                    memory_id=source.memory_id, revision=source.revision, recorded_at=source.created_at,
                    evidence=evidence,
                ),
            ))
        if not grounded:
            return ExtractionResult(source_id=source.source_id, candidates=(), rejections=tuple(rejections))
        checked = await self.verify_candidates(source, tuple(grounded))
        supported: set[str] = set()
        for verdict in checked.verdicts:
            if (verdict.verdict == "supported") != (verdict.reason == "supported"):
                raise IntelligenceError("invalid_verification")
            if verdict.verdict == "supported":
                supported.add(verdict.candidate_id)
            else:
                rejections.append(Rejection(candidate_id=verdict.candidate_id, reason=verdict.reason))
        return ExtractionResult(
            source_id=source.source_id,
            candidates=tuple(candidate for candidate in grounded if candidate.candidate_id in supported),
            rejections=tuple(rejections),
        )

    async def verify_candidates(
        self, source: SourceDocument, candidates: tuple[FactCandidate, ...],
    ) -> VerificationBatch:
        """Reusable source/semantic check; this never authorizes human approval."""
        expected = {candidate.candidate_id for candidate in candidates}
        if len(expected) != len(candidates) or not 1 <= len(candidates) <= 20:
            raise IntelligenceError("invalid_verification")
        try:
            for candidate in candidates:
                origin = candidate.provenance
                if (origin.source_id != source.source_id or origin.source_sha256 != source.content_sha256
                    or origin.memory_id != source.memory_id or origin.revision != source.revision
                    or origin.recorded_at != source.created_at
                    or origin.evidence != _evidence(source, candidate.fact)
                    or candidate.subject_scope != _scope(source, candidate.fact)):
                    raise ValueError
        except Exception:
            raise IntelligenceError("invalid_verification") from None
        checked = await self._structured(VERIFY_PROMPT, {
            "source": source.model_dump(mode="json"),
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        }, VerificationBatch)
        actual = [verdict.candidate_id for verdict in checked.verdicts]
        if len(actual) != len(expected) or set(actual) != expected or any(
            (verdict.verdict == "supported") != (verdict.reason == "supported")
            for verdict in checked.verdicts
        ):
            raise IntelligenceError("invalid_verification")
        return checked

    async def preview(
        self, source: SourceDocument, references: Sequence[SourceDocument] = (), *, top_k: int = 5,
    ) -> PreviewResult:
        sources = (source, *references)
        if len({document.source_id for document in sources}) != len(sources):
            raise IntelligenceError("invalid_input")
        self.validate_top_k(top_k)
        extractions = tuple([await self.extract(document) for document in sources])
        return await self.compare(extractions, top_k=top_k)

    @staticmethod
    def validate_top_k(top_k: int) -> None:
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise IntelligenceError("invalid_input")

    async def _vectors(self, candidates: list[FactCandidate]) -> list[tuple[float, ...]]:
        values: list[tuple[float, ...]] = []
        try:
            # Bounded batches retain the provider's native BGE dimension.
            for start in range(0, len(candidates), 20):
                batch = candidates[start:start + 20]
                result = await asyncio.wait_for(embed(
                    provider=self._bge, request=EmbeddingRequest(inputs=tuple(c.fact.text for c in batch)),
                ), timeout=EMBEDDING_TIMEOUT_SECONDS)
                if result.provider_id != "ollama" or result.model_id != "bge-m3:567m-fp16" or result.dimension != 1024:
                    raise ValueError
                if len(result.embeddings) != len(batch):
                    raise ValueError
                for embedding in result.embeddings:
                    vector = tuple(embedding.values)
                    norm = math.sqrt(sum(value * value for value in vector))
                    if len(vector) != 1024 or not math.isfinite(norm) or norm <= 0:
                        raise ValueError
                    values.append(tuple(value / norm for value in vector))
            return values
        except Exception:
            raise IntelligenceError("embedding_failed") from None

    async def compare(
        self, extractions: tuple[ExtractionResult, ...], *, top_k: int = 5,
        comparison_scope: str = "local_sources",
    ) -> PreviewResult:
        self.validate_top_k(top_k)
        if not extractions or len({e.source_id for e in extractions}) != len(extractions):
            raise IntelligenceError("invalid_input")
        candidates = [candidate for extraction in extractions for candidate in extraction.candidates]
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise IntelligenceError("invalid_input")
        primary = len(extractions[0].candidates)
        pairs = [(i, j) for i in range(primary) for j in range(i + 1, len(candidates))]
        selected: dict[tuple[int, int], float | None] = {}
        for i, j in pairs:
            if _context_guard(candidates[i], candidates[j]) is None and _exact(candidates[i], candidates[j]):
                selected[i, j] = None
        if pairs:
            vectors = await self._vectors(candidates)
            for i in range(primary):
                ranked = sorted((
                    (sum(a * b for a, b in zip(vectors[i], vectors[j])), j)
                    for j in range(i + 1, len(candidates))
                ), key=lambda value: (-value[0], value[1]))
                for score, j in ranked[:top_k]:
                    selected[i, j] = max(-1.0, min(1.0, score))
        relations: list[RelationSuggestion] = []
        pending: list[tuple[str, FactCandidate, FactCandidate, float | None]] = []
        for (i, j), similarity in sorted(selected.items()):
            left, right = candidates[i], candidates[j]
            guarded = _context_guard(left, right)
            relation = guarded or ("exact_duplicate" if _exact(left, right) else None)
            if relation is not None:
                relations.append(RelationSuggestion(
                    left_id=left.candidate_id, right_id=right.candidate_id, relation=relation,
                    similarity=similarity, rationale="Context guard" if guarded else "Identical claim and context",
                ))
            else:
                pending.append((f"pair-{i}-{j}", left, right, similarity))
        for start in range(0, len(pending), 20):
            batch = pending[start:start + 20]
            result = await self._structured(RELATE_PROMPT, {"pairs": [
                {"pair_id": pair_id, "left": left.model_dump(mode="json"),
                 "right": right.model_dump(mode="json"), "similarity_hint": score}
                for pair_id, left, right, score in batch
            ]}, PairBatch)
            supplied = {item[0]: item for item in batch}
            verdict_ids = [verdict.pair_id for verdict in result.verdicts]
            if len(verdict_ids) != len(supplied) or set(verdict_ids) != set(supplied):
                raise IntelligenceError("invalid_relations")
            for verdict in result.verdicts:
                _, left, right, score = supplied[verdict.pair_id]
                replacement = verdict.replacement_candidate_id
                relation, rationale = verdict.relation, verdict.rationale
                notes: tuple[str, ...] = ()
                if verdict.relation == "possible_supersession":
                    if replacement not in {left.candidate_id, right.candidate_id}:
                        raise IntelligenceError("invalid_relations")
                    target = left if replacement == left.candidate_id else right
                    if not _explicit_replacement(target):
                        relation, replacement = "related", None
                        rationale = "The source does not explicitly establish a replacement; retain both contexts."
                        notes = ("replacement_not_explicit",)
                elif replacement is not None:
                    raise IntelligenceError("invalid_relations")
                if relation == "exact_duplicate" and not _exact(left, right):
                    relation = "semantic_duplicate"
                if relation in {"exact_duplicate", "semantic_duplicate"} and (
                    left.fact.polarity != right.fact.polarity
                    or left.fact.temporal_context != right.fact.temporal_context
                ):
                    relation = "uncertain"
                    rationale = "Equivalence requires verification of polarity and temporal context."
                    notes += ("equivalence_context_mismatch",)
                relations.append(RelationSuggestion(
                    left_id=left.candidate_id, right_id=right.candidate_id, relation=relation,
                    similarity=score, rationale=rationale, replacement_candidate_id=replacement,
                    validation_notes=notes,
                ))
        return PreviewResult(
            extractions=extractions, relations=tuple(relations), comparison_scope=comparison_scope,
            exhaustive=comparison_scope == "local_sources" and len(selected) == len(pairs),
            compared_pairs=len(selected), available_pairs=len(pairs), top_k=top_k,
        )
