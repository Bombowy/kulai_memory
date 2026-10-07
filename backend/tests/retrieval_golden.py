"""Golden v1: frozen before any real retrieval run; synthetic data only."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import NAMESPACE_URL, UUID, uuid5

from kulai_memory.application import Memory


def golden_id(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kulai-memory-golden-v1:{label}")


_DOCUMENTS = (
    ("M1", "Android voice client sends PCM audio over WebSocket."),
    ("M2", "Mobile retry keeps the same ingestion identifier."),
    ("M3", "ADB reverse connects the phone to the localhost backend over USB."),
    ("M4", "Voice activity detection rejects silence without creating Memory."),
    ("C1", "Tomato soup needs basil and garlic."),
    ("C2", "Pancakes can be reheated in an air fryer."),
    ("C3", "Salmon is planned for dinner."),
    ("C4", "Bread is baked in an oven at 200 degrees Celsius."),
    ("G1", "Jalapeño grows in the hydroponic bucket."),
    ("G2", "Greenhouse lighting uses LED panels."),
    ("G3", "Pepper seedlings need nutrient solution."),
    ("G4", "The garden water pH is checked every morning."),
    ("W1", "The customer requested CRM integration."),
    ("W2", "The landing page project needs responsive design."),
    ("W3", "Invoice automation uses an API."),
    ("W4", "The client project deadline is Wednesday."),
)
GOLDEN_MEMORIES = tuple(Memory(
    id=golden_id(label), ingestion_id=golden_id(f"ingestion:{label}"), content=content,
    created_at=datetime(2025, 1, 1, tzinfo=UTC),
) for label, content in _DOCUMENTS)


@dataclass(frozen=True, slots=True)
class GoldenQuery:
    id: str
    text: str
    expected: frozenset[UUID]
    language: str


_QUERY_PAIRS = (
    ("M3", "How does the phone connect to the localhost backend over USB?",
     "Jak telefon łączy się z lokalnym backendem przez USB?"),
    ("M2", "What identifier stays unchanged when retrying a mobile note?",
     "Jaki identyfikator pozostaje taki sam przy ponawianiu notatki mobilnej?"),
    ("C2", "What food can be reheated in an air fryer?",
     "Jakie jedzenie można odgrzać we frytkownicy beztłuszczowej?"),
    ("C1", "What herbs and seasoning does tomato soup need?",
     "Jakich ziół i przypraw potrzebuje zupa pomidorowa?"),
    ("G1", "What pepper grows in the hydroponic bucket?",
     "Jaka papryka rośnie w wiadrze hydroponicznym?"),
    ("G2", "What lights are used in the greenhouse?",
     "Jakie oświetlenie jest używane w szklarni?"),
    ("W1", "What customer relationship system integration did the customer request?",
     "O integrację z jakim systemem zarządzania relacjami z klientami poprosił klient?"),
    ("W3", "What technology is used to automate invoices?",
     "Jaka technologia służy do automatyzacji faktur?"),
)
GOLDEN_QUERIES = tuple(
    GoldenQuery(id=f"{label}_{language}", text=text, expected=frozenset({golden_id(label)}),
                language=language)
    for label, english, polish in _QUERY_PAIRS
    for language, text in (("en", english), ("pl", polish))
)
GOLDEN_THRESHOLDS = MappingProxyType({1: 0.60, 3: 0.80, 5: 0.90})


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr_at_5: float


def evaluate_queries(
    queries: tuple[GoldenQuery, ...], rankings: Mapping[str, tuple[UUID, ...]],
) -> RetrievalMetrics:
    """Macro recall; each relevant ID counts once. MRR is truncated at five."""

    if not queries or any(not query.expected for query in queries):
        raise ValueError("Evaluation requires nonempty queries and relevance sets.")
    if len({query.id for query in queries}) != len(queries) or set(rankings) != {q.id for q in queries}:
        raise ValueError("Evaluation requires exactly one ranking for every query.")
    recalls = {k: 0.0 for k in (1, 3, 5)}
    reciprocal = 0.0
    for query in queries:
        ids = rankings[query.id]
        for k in recalls:
            recalls[k] += len(query.expected.intersection(ids[:k])) / len(query.expected)
        reciprocal += next((1.0 / rank for rank, value in enumerate(ids[:5], start=1)
                            if value in query.expected), 0.0)
    count = len(queries)
    return RetrievalMetrics(recalls[1] / count, recalls[3] / count, recalls[5] / count,
                            reciprocal / count)


def dataset_sha256() -> str:
    payload = {
        "documents": _DOCUMENTS,
        "queries": [(q.id, q.text, sorted(str(i) for i in q.expected)) for q in GOLDEN_QUERIES],
        "thresholds": dict(GOLDEN_THRESHOLDS),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
