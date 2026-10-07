from __future__ import annotations

from uuid import UUID

import pytest

from backend.tests.retrieval_golden import (
    GOLDEN_MEMORIES, GOLDEN_QUERIES, GOLDEN_THRESHOLDS, GoldenQuery,
    dataset_sha256, evaluate_queries, golden_id,
)


def test_golden_v1_has_fixed_topics_languages_identities_and_thresholds():
    assert len(GOLDEN_MEMORIES) == len(GOLDEN_QUERIES) == 16
    assert len({m.id for m in GOLDEN_MEMORIES}) == 16
    assert len([q for q in GOLDEN_QUERIES if q.language == "en"]) == 8
    assert len([q for q in GOLDEN_QUERIES if q.language == "pl"]) == 8
    assert all(q.expected.issubset({m.id for m in GOLDEN_MEMORIES}) for q in GOLDEN_QUERIES)
    assert GOLDEN_MEMORIES[0].id == golden_id("M1")
    assert dict(GOLDEN_THRESHOLDS) == {1: 0.60, 3: 0.80, 5: 0.90}
    assert dataset_sha256() == "73489b2a359a444074a7aa191337de00d7dcdeea86c741dd59d6e5c7c285bdf7"


def test_macro_recall_for_single_multiple_and_missing_relevant_items():
    a, b, c, unrelated = (UUID(int=i) for i in range(1, 5))
    queries = (
        GoldenQuery("single", "synthetic", frozenset({a}), "en"),
        GoldenQuery("multiple", "synthetic", frozenset({b, c}), "en"),
        GoldenQuery("missing", "synthetic", frozenset({a}), "en"),
    )
    result = evaluate_queries(queries, {
        "single": (unrelated, a), "multiple": (b, unrelated, c), "missing": (),
    })
    assert result.recall_at_1 == pytest.approx(1 / 6)
    assert result.recall_at_3 == result.recall_at_5 == pytest.approx(2 / 3)
    assert result.mrr_at_5 == pytest.approx(0.5)


def test_duplicate_relevant_id_counts_once_and_mrr_is_truncated_at_five():
    a, b, unrelated = (UUID(int=i) for i in range(1, 4))
    queries = (GoldenQuery("one", "synthetic", frozenset({a, b}), "en"),)
    result = evaluate_queries(queries, {"one": (a, a, a, a, a, b)})
    assert result.recall_at_1 == result.recall_at_3 == result.recall_at_5 == 0.5
    assert result.mrr_at_5 == 1.0
    missing = evaluate_queries(queries, {"one": (unrelated,) * 5 + (a,)})
    assert missing.mrr_at_5 == missing.recall_at_5 == 0.0


@pytest.mark.parametrize("queries,rankings", [
    ((), {}), ((GoldenQuery("x", "synthetic", frozenset(), "en"),), {"x": ()}),
    (GOLDEN_QUERIES, {}), (GOLDEN_QUERIES, {q.id: () for q in GOLDEN_QUERIES} | {"extra": ()}),
])
def test_incomplete_or_undefined_evaluation_is_rejected(queries, rankings):
    with pytest.raises(ValueError):
        evaluate_queries(queries, rankings)
