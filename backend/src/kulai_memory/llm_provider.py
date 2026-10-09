"""Loopback-only construction of one reusable Ollama LLM provider."""

from ipaddress import ip_address
from urllib.parse import urlsplit

from kulai_provider_ollama import OllamaLLMProvider, OllamaProviderConfig

from .application.rag import LLM_TIMEOUT_SECONDS, MemoryRagError
from .settings import Settings


def create_llm_provider(*, settings: Settings) -> OllamaLLMProvider:
    try:
        config = OllamaProviderConfig(
            model=settings.kulai_llm_model, host=settings.kulai_ollama_base_url,
            timeout_seconds=LLM_TIMEOUT_SECONDS, think=False, num_ctx=32768,
        )
        hostname = urlsplit(config.host).hostname
        loopback = hostname == "localhost"
        if hostname and not loopback:
            loopback = ip_address(hostname).is_loopback
        if not loopback:
            raise ValueError
        return OllamaLLMProvider(config)
    except Exception:
        raise MemoryRagError("rag.invalid_configuration") from None
