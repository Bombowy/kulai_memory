"""Host-local construction of the reusable Ollama embedding provider."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from kulai_embeddings import EmbeddingValidationError
from kulai_provider_ollama_embeddings import (
    OllamaEmbeddingProvider,
    OllamaEmbeddingProviderConfig,
)

from .settings import Settings, get_settings


def create_embedding_provider(
    *, settings: Settings | None = None
) -> OllamaEmbeddingProvider:
    """Create one local provider without changing its native dimensions."""

    active_settings = settings or get_settings()
    try:
        config = OllamaEmbeddingProviderConfig(
            model=active_settings.kulai_embedding_model,
            host=active_settings.kulai_ollama_base_url,
        )
    except (TypeError, ValueError):
        raise EmbeddingValidationError(
            "Embedding provider configuration is invalid."
        ) from None

    hostname = urlsplit(config.host).hostname
    is_loopback = hostname == "localhost"
    if hostname is not None and not is_loopback:
        try:
            is_loopback = ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise EmbeddingValidationError(
            "Ollama embedding host must use a loopback address."
        )

    return OllamaEmbeddingProvider(config)
