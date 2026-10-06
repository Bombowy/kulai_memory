"""Validate host dimension compatibility after provider-neutral embedding."""

from __future__ import annotations

from kulai_embeddings import EmbeddingResponse, EmbeddingValidationError


def validate_embedding_dimension(
    response: EmbeddingResponse, *, expected_dimension: int | None
) -> None:
    """Require an explicit host dimension matching the native response."""

    if (
        isinstance(expected_dimension, bool)
        or not isinstance(expected_dimension, int)
        or expected_dimension < 1
    ):
        raise EmbeddingValidationError(
            "Vector dimension must be explicitly configured as a positive integer."
        )
    if response.dimension != expected_dimension:
        raise EmbeddingValidationError(
            "Embedding dimension does not match configured vector dimension.",
            public_details={
                "expected_dimension": expected_dimension,
                "actual_dimension": response.dimension,
            },
        )
