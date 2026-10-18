"""Opt-in Memory intelligence preview; no automatic ingestion hooks."""

from .models import (
    ExtractionResult, FactCandidate, IntelligenceError, PreviewResult,
    RelationSuggestion, SourceDocument,
)
from .service import MemoryIntelligenceService

__all__ = [
    "ExtractionResult", "FactCandidate", "IntelligenceError", "PreviewResult",
    "RelationSuggestion", "SourceDocument", "MemoryIntelligenceService",
]
