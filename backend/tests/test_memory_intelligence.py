from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_llm import LLMCapabilities, LLMResponse
from pydantic import ValidationError

from kulai_memory.application.intelligence import IntelligenceError, MemoryIntelligenceService, SourceDocument
from kulai_memory.application.intelligence.models import DraftFact
from kulai_memory.application.intelligence.prompts import EXTRACT_PROMPT, RELATE_PROMPT, VERIFY_PROMPT

GOLDEN_PATH = Path(__file__).parent / "fixtures/memory_intelligence_golden.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in GOLDEN["cases"]}
PRIVATE = "PRIVATE_FACT_PASSWORD_SENTINEL"


class GoldenLLM:
    provider_id = "ollama"
    capabilities = LLMCapabilities(supports_system_messages=True, supports_json_schema=True,
                                   supports_temperature=True, supports_max_output_tokens=True)

    def __init__(self, cases=None, relation="semantic_duplicate", replacement_side="right"):
        self.cases = cases or list(CASES.values())
        self.relation = relation
        self.replacement_side = replacement_side
        self.calls = []
        self.mutate = None
        self.closed = 0

    async def generate(self, request):
        self.calls.append(request)
        assert request.response_format.type.value == "json_schema"
        prompt = request.messages[0].content
        payload = json.loads(request.messages[1].content)
        if prompt == EXTRACT_PROMPT:
            case = next(case for case in self.cases if case["text"] == payload["source"]["text"])
            output = {"candidates": copy.deepcopy(case["drafts"])}
        elif prompt == VERIFY_PROMPT:
            output = {"verdicts": [dict(candidate_id=c["candidate_id"], verdict="supported", reason="supported")
                                   for c in payload["candidates"]]}
        else:
            assert prompt == RELATE_PROMPT
            output = {"verdicts": [dict(
                pair_id=p["pair_id"], relation=self.relation, rationale="Synthetic relation evaluation.",
                replacement_candidate_id=p[self.replacement_side]["candidate_id"] if self.relation == "possible_supersession" else None,
            ) for p in payload["pairs"]]}
        if self.mutate:
            output = self.mutate(prompt, output)
        text = output if isinstance(output, str) else json.dumps(output)
        return LLMResponse(text=text, provider_id="ollama", model_id="qwen3.5:9b")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed += 1


class ConstantBGE:
    """Deliberately identical embeddings: semantic truth must come from elsewhere."""
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities(supports_batch=True)

    def __init__(self):
        self.calls = []
        self.closed = 0
        self.dimension = 1024

    async def embed(self, request):
        self.calls.append(request)
        return EmbeddingResponse(
            provider_id="ollama", model_id="bge-m3:567m-fp16", dimension=self.dimension,
            embeddings=tuple(EmbeddingVector(values=(1.0,) + (0.0,) * (self.dimension - 1)) for _ in request.inputs),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed += 1


def service(llm=None, bge=None):
    return MemoryIntelligenceService(llm_provider=llm or GoldenLLM(), embedding_provider=bge or ConstantBGE())


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["id"])
def test_golden_protocol_extraction_and_exact_provenance(case):
    source = SourceDocument(source_id=case["id"], text=case["text"])
    result = asyncio.run(service().extract(source))
    assert len(result.candidates) == len(case["drafts"])
    assert not result.rejections
    for candidate, expected in zip(result.candidates, case["drafts"]):
        assert candidate.fact.model_dump() == expected
        assert candidate.review_status == "requires_review"
        assert candidate.provenance.source_sha256 == source.content_sha256
        assert candidate.provenance.memory_id is candidate.provenance.revision is None
        for span in candidate.provenance.evidence:
            assert source.text[span.start:span.end] == span.quote


@pytest.mark.parametrize("pair", GOLDEN["pairs"], ids=lambda p: p["id"])
def test_golden_pair_protocol_and_context_guards(pair):
    a, b = CASES[pair["left"]], CASES[pair["right"]]
    speaker = "shared-synthetic-author" if pair["shared_author"] else None
    llm, bge = GoldenLLM(relation=pair["expected"]), ConstantBGE()
    result = asyncio.run(service(llm, bge).preview(
        SourceDocument(source_id="a", text=a["text"], speaker_id=speaker),
        (SourceDocument(source_id="b", text=b["text"], speaker_id=speaker),),
    ))
    assert result.dry_run and result.exhaustive and result.compared_pairs == 1
    assert result.relations[0].relation == pair["expected"]
    assert result.relations[0].review_status == "requires_review"
    if pair["expected"] == "possible_supersession":
        assert result.relations[0].replacement_candidate_id == "b:1"
    assert bge.calls and not bge.calls[0].model_hint


def test_empty_input_calls_neither_provider():
    llm, bge = GoldenLLM(), ConstantBGE()
    result = asyncio.run(service(llm, bge).preview(SourceDocument(source_id="empty", text=" \r\n")))
    assert not result.extractions[0].candidates and not llm.calls and not bge.calls


@pytest.mark.parametrize("invalid", [
    "not JSON", '{"candidates":[],"candidates":[]}', '{"candidates":[],"extra":1}',
    '{"candidates":NaN}', '{"candidates":{}}',
])
def test_strict_json_errors_do_not_become_empty_success_or_leak(invalid):
    llm = GoldenLLM()
    llm.mutate = lambda prompt, output: invalid
    with pytest.raises(IntelligenceError) as caught:
        asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=CASES["pl_flask"]["text"])))
    assert caught.value.code == "intelligence.generation_failed"
    assert invalid not in str(caught.value)


@pytest.mark.parametrize("field,value", [
    ("subject_is_author", "false"), ("information_type", "confirmed"),
    ("polarity", "maybe"), ("evidence_quotes", "a quotation"), ("extra", True),
])
def test_strict_candidate_schema(field, value):
    draft = copy.deepcopy(CASES["pl_flask"]["drafts"][0])
    draft[field] = value
    with pytest.raises(ValidationError):
        DraftFact.model_validate(draft, strict=True)


@pytest.mark.parametrize("corruption,reason", [
    (dict(temporal_context="in 2099"), "unsupported_time"),
    (dict(technologies=["Kubernetes"]), "unsupported_technology"),
    (dict(project_context="Secret project"), "unsupported_project"),
])
def test_unsupported_evidence_rejected_before_semantic_verifier(corruption, reason):
    case = copy.deepcopy(CASES["pl_flask"])
    case["drafts"][0].update(corruption)
    llm = GoldenLLM([case])
    result = asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=case["text"])))
    assert not result.candidates and result.rejections[0].reason == reason
    assert len(llm.calls) == 1 and PRIVATE not in repr(result)


def test_quote_schema_rejects_fabricated_quotes_before_verification():
    case = copy.deepcopy(CASES["pl_flask"])
    case["drafts"][0]["evidence_quotes"] = [PRIVATE]
    llm = GoldenLLM([case])
    with pytest.raises(IntelligenceError, match="generation_failed"):
        asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=case["text"])))
    assert len(llm.calls) == 1


@pytest.mark.parametrize("reason", ["not_entailed", "wrong_type", "wrong_subject", "lost_negation", "instruction"])
def test_verbatim_quote_does_not_accept_unsupported_claim(reason):
    llm = GoldenLLM()
    def mutate(prompt, output):
        if prompt == VERIFY_PROMPT:
            output["verdicts"][0].update(verdict="unsupported", reason=reason)
        return output
    llm.mutate = mutate
    result = asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=CASES["pl_flask"]["text"])))
    assert not result.candidates and result.rejections[0].reason == reason


@pytest.mark.parametrize("mode", ["missing", "unknown", "duplicate", "inconsistent"])
def test_verifier_must_cover_exact_supplied_ids(mode):
    llm = GoldenLLM()
    def mutate(prompt, output):
        if prompt == VERIFY_PROMPT:
            if mode == "missing": output["verdicts"] = []
            if mode == "unknown": output["verdicts"][0]["candidate_id"] = PRIVATE
            if mode == "duplicate": output["verdicts"] *= 2
            if mode == "inconsistent": output["verdicts"][0]["reason"] = "not_entailed"
        return output
    llm.mutate = mutate
    with pytest.raises(IntelligenceError, match="invalid_verification"):
        asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=CASES["pl_flask"]["text"])))


def test_repeated_quotes_unicode_crlf_and_memory_origin_are_preserved():
    case = copy.deepcopy(CASES["pl_flask"])
    quote = case["text"]
    case["text"] = quote + "\r\n" + quote
    source = SourceDocument(source_id="mem", text=case["text"], memory_id=UUID(int=1), revision=7,
                            created_at=datetime(2026, 10, 1, tzinfo=UTC))
    result = asyncio.run(service(GoldenLLM([case])).extract(source))
    provenance = result.candidates[0].provenance
    assert [span.start for span in provenance.evidence] == [0, len(quote) + 2]
    assert provenance.memory_id == UUID(int=1) and provenance.revision == 7
    assert provenance.recorded_at == source.created_at


@pytest.mark.parametrize("kwargs", [dict(memory_id=UUID(int=1)), dict(revision=1),
    dict(revision=True, memory_id=UUID(int=1)), dict(created_at=datetime(2026, 1, 1)), dict(text="x" * 12001)])
def test_invalid_sources_are_strict(kwargs):
    values = dict(source_id="a", text=PRIVATE)
    values.update(kwargs)
    with pytest.raises(ValidationError) as caught:
        SourceDocument(**values)
    assert PRIVATE not in str(caught.value)


def test_injection_never_enters_system_policy_and_is_not_a_command():
    llm = GoldenLLM()
    case = CASES["pl_injection"]
    result = asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=case["text"])))
    assert not result.candidates
    assert case["text"] not in llm.calls[0].messages[0].content
    assert json.loads(llm.calls[0].messages[1].content)["source"]["text"] == case["text"]


def test_perfect_cosine_does_not_establish_equivalence_across_projects():
    result = asyncio.run(service(GoldenLLM(relation="semantic_duplicate")).preview(
        SourceDocument(source_id="x", text=CASES["pl_flask"]["text"]),
        (SourceDocument(source_id="y", text=CASES["pl_other"]["text"]),),
    ))
    assert result.relations[0].similarity == 1.0 and result.relations[0].relation == "unrelated"


def test_bad_bge_dimension_is_safe():
    bge = ConstantBGE()
    bge.dimension = 768
    with pytest.raises(IntelligenceError, match="embedding_failed"):
        asyncio.run(service(bge=bge).preview(SourceDocument(source_id="a", text=CASES["pl_multi"]["text"])))


@pytest.mark.parametrize("mode", ["missing", "unknown", "duplicate", "wrong_replacement"])
def test_relation_response_cannot_invent_ids(mode):
    llm = GoldenLLM(relation="possible_supersession")
    def mutate(prompt, output):
        if prompt == RELATE_PROMPT:
            if mode == "missing": output["verdicts"] = []
            if mode == "unknown": output["verdicts"][0]["pair_id"] = PRIVATE
            if mode == "duplicate": output["verdicts"] *= 2
            if mode == "wrong_replacement": output["verdicts"][0]["replacement_candidate_id"] = PRIVATE
        return output
    llm.mutate = mutate
    with pytest.raises(IntelligenceError, match="invalid_relations"):
        asyncio.run(service(llm).preview(SourceDocument(source_id="a", text=CASES["pl_flask"]["text"]),
            (SourceDocument(source_id="b", text=CASES["pl_switch"]["text"]),)))


def test_timeout_and_cancellation(monkeypatch):
    from kulai_memory.application.intelligence import service as module
    class Slow(GoldenLLM):
        async def generate(self, request):
            await asyncio.Event().wait()
    monkeypatch.setattr(module, "LLM_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(IntelligenceError, match="generation_failed"):
        asyncio.run(service(Slow()).extract(SourceDocument(source_id="a", text=PRIVATE)))
    class Cancelled(GoldenLLM):
        async def generate(self, request):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service(Cancelled()).extract(SourceDocument(source_id="a", text=PRIVATE)))


def test_portable_core_has_no_database_transport_or_file_io():
    import ast
    from kulai_memory.application import intelligence
    forbidden = {"sqlalchemy", "fastapi", "pathlib", "subprocess", "kulai_db"}
    for path in Path(intelligence.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden for alias in node.names)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name): assert node.func.id != "open"
                if isinstance(node.func, ast.Attribute): assert node.func.attr not in {"commit", "execute", "upsert", "create_all", "unlink"}


def test_model_cannot_propose_approval_or_extra_canonical_write_fields():
    llm = GoldenLLM()
    def mutate(prompt, output):
        if prompt == EXTRACT_PROMPT:
            output["candidates"][0].update(review_status="approved", memory_id=str(UUID(int=1)))
        return output
    llm.mutate = mutate
    with pytest.raises(IntelligenceError, match="generation_failed"):
        asyncio.run(service(llm).extract(SourceDocument(source_id="a", text=CASES["pl_flask"]["text"])))


def test_real_observed_false_positive_disjoint_periods_cannot_imply_replacement():
    # Observed with real Qwen: Flask in 2024 + FastAPI in 2025 -> proposed supersession.
    # Reproduce the wrong model verdict deterministically; do not treat it as evidence.
    result = asyncio.run(service(GoldenLLM(relation="possible_supersession")).preview(
        SourceDocument(source_id="old", text=CASES["pl_period"]["text"]),
        (SourceDocument(source_id="later", text=CASES["en_period"]["text"]),),
    ))
    relation = result.relations[0]
    assert relation.relation == "related" and relation.replacement_candidate_id is None
    assert relation.validation_notes == ("replacement_not_explicit",)
