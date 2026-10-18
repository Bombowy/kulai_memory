"""Opt-in real Qwen/BGE quality evaluation, exclusively on the synthetic corpus."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.request import urlopen

import pytest

from backend.tests.test_memory_intelligence import GOLDEN, GOLDEN_PATH
from kulai_memory.application.intelligence import IntelligenceError, SourceDocument
from kulai_memory.memory_intelligence_runtime import MemoryIntelligenceRuntime
from kulai_memory.settings import Settings


def _signature(candidate):
    fact = candidate.fact
    return fact.information_type, frozenset(t.casefold() for t in fact.technologies)


def test_real_models_synthetic_quality_and_exact_sources():
    if not all(os.environ.get(name) == "1" for name in ("KULAI_RUN_OLLAMA_INTEGRATION", "KULAI_RUN_LLM_INTEGRATION")):
        pytest.skip("NOT RUN: enable local Ollama and LLM integration flags.")
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
            models = {model["name"] for model in json.load(response)["models"]}
    except Exception:
        pytest.skip("NOT RUN: local Ollama unavailable; no model download attempted.")
    if not {"qwen3.5:9b", "bge-m3:567m-fp16"} <= models:
        pytest.skip("NOT RUN: required installed local models unavailable.")

    async def scenario():
        report = {"dataset_sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
                  "models": ["qwen3.5:9b", "bge-m3:567m-fp16"], "cases": [], "pairs": []}
        extracted = {}
        selected_pairs = GOLDEN["pairs"]
        async with MemoryIntelligenceRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024)) as runtime:
            for case in GOLDEN["cases"]:
                source = SourceDocument(source_id=case["id"], text=case["text"])
                try:
                    result = await runtime._service.extract(source)
                except IntelligenceError as exc:
                    report["cases"].append({"id": case["id"], "error": exc.code, "annotation_match": False})
                    print(f"real_extraction={case['id']};status=FAIL;code={exc.code}", flush=True)
                    continue
                extracted[case["id"]] = result
                expected = [(d["information_type"], frozenset(t.casefold() for t in d["technologies"])) for d in case["drafts"]]
                actual = [_signature(c) for c in result.candidates]
                match = sorted((a, sorted(b)) for a, b in actual) == sorted((a, sorted(b)) for a, b in expected)
                for candidate in result.candidates:
                    assert candidate.review_status == "requires_review"
                    for span in candidate.provenance.evidence:
                        assert source.text[span.start:span.end] == span.quote
                report["cases"].append({"id": case["id"], "annotation_match": match,
                                        "result": result.model_dump(mode="json")})
                reasons = ",".join(r.reason for r in result.rejections)
                print(f"real_extraction={case['id']};candidates={len(actual)};rejections={len(result.rejections)};reasons={reasons};annotation_match={str(match).lower()}", flush=True)
            for pair in selected_pairs:
                a, b = extracted.get(pair["left"]), extracted.get(pair["right"])
                # Atomic single-claim pair fixtures. Missing candidates are measured as misses.
                if a is None or b is None or not a.candidates or not b.candidates:
                    report["pairs"].append({"id": pair["id"], "expected": pair["expected"], "actual": [], "match": False})
                    continue
                if pair["shared_author"]:
                    def shared_author(extraction):
                        return extraction.model_copy(update={"candidates": tuple(
                            c.model_copy(update={"subject_scope": "author:explicit-synthetic-author"})
                            if c.fact.subject_is_author else c for c in extraction.candidates
                        )})
                    a, b = shared_author(a), shared_author(b)
                try:
                    preview = await runtime._service.compare((a, b), top_k=20)
                    actual = [r.relation for r in preview.relations]
                    match = pair["expected"] in actual
                    report["pairs"].append({"id": pair["id"], "expected": pair["expected"], "actual": actual,
                                            "match": match, "preview": preview.model_dump(mode="json")})
                    print(f"real_pair={pair['id']};expected={pair['expected']};actual={','.join(actual)};match={str(match).lower()}", flush=True)
                except IntelligenceError as exc:
                    report["pairs"].append({"id": pair["id"], "expected": pair["expected"], "error": exc.code, "match": False})
            # Quality misses remain explicit measurements; they are never converted into asserted accuracy.
            report["summary"] = {
                "extraction_annotation_matches": sum(c["annotation_match"] for c in report["cases"]),
                "extraction_cases": len(report["cases"]),
                "pair_matches": sum(p["match"] for p in report["pairs"]), "pair_cases": len(report["pairs"]),
            }
        destination = os.environ.get("KULAI_INTELLIGENCE_EVALUATION_OUTPUT")
        if destination:
            target = Path(destination).resolve()
            if target.is_relative_to(GOLDEN_PATH.resolve().parents[3]):
                pytest.fail("Evaluation artifact must be outside the repository.")
            target.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        print("real_intelligence_summary=" + json.dumps(report["summary"], sort_keys=True), flush=True)
        assert report["cases"] and report["pairs"]
        unsafe = [case for case in report["cases"] if case["id"].endswith("_injection")
                  and case.get("result", {}).get("candidates")]
        assert not unsafe, "Prompt-injection source yielded accepted candidates; inspect synthetic evaluation."
    asyncio.run(scenario())
