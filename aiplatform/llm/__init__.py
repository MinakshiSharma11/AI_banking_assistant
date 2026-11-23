"""LLM gateway: one interface over providers, chosen by LLM_PROVIDER."""

from __future__ import annotations

from aiplatform.llm.base import (
    EmbeddingProvider,
    LLMError,
    LLMProvider,
    LLMResult,
    Message,
    ToolCall,
    ToolSpec,
    inline_refs,
)
from aiplatform.llm.config import get_llm_settings

PROVIDERS = ("ollama", "anthropic", "gemini", "cohere")
LOCAL_PROVIDERS = frozenset({"ollama"})  # nothing leaves the machine


class ProviderNotConfigured(LLMError):
    pass


def get_provider(name: str | None = None) -> LLMProvider:
    s = get_llm_settings()
    name = (name or s.provider).lower()
    if name == "ollama":
        from aiplatform.llm.ollama import OllamaProvider

        return OllamaProvider(
            s.ollama_base_url,
            s.ollama_model,
            s.ollama_think,
            s.ollama_timeout_s,
            api_key=s.ollama_api_key,
        )
    if name == "anthropic":
        _need(s.anthropic_api_key, "ANTHROPIC_API_KEY")
        from aiplatform.llm.anthropic import AnthropicProvider

        return AnthropicProvider(s.anthropic_api_key, s.anthropic_model, s.timeout_s)
    if name == "gemini":
        _need(s.gemini_api_key and s.gemini_model, "GEMINI_API_KEY and GEMINI_MODEL")
        from aiplatform.llm.gemini import GeminiProvider

        return GeminiProvider(
            s.gemini_api_key, s.gemini_model, s.timeout_s, api_version=s.gemini_api_version
        )
    if name == "cohere":
        _need(s.cohere_api_key and s.cohere_model, "COHERE_API_KEY and COHERE_MODEL")
        from aiplatform.llm.cohere import CohereProvider

        return CohereProvider(s.cohere_api_key, s.cohere_model, s.timeout_s)
    raise ProviderNotConfigured(f"Unknown LLM_PROVIDER '{name}'. Use one of {PROVIDERS}.")


def get_embedder() -> EmbeddingProvider | None:
    """Embedding model for few-shot example retrieval (any OpenAI-compatible endpoint).
    None when EMBED_MODEL is empty: retrieval then falls back to word overlap."""
    s = get_llm_settings()
    if not s.embed_model:
        return None
    from aiplatform.llm.ollama import OllamaEmbedder

    return OllamaEmbedder(
        s.embed_base_url, s.embed_model, s.embed_query_instruction, api_key=s.embed_api_key
    )


def _need(value: object, what: str) -> None:
    if not value:
        raise ProviderNotConfigured(f"Set {what} in .env to use this provider.")


__all__ = [
    "LOCAL_PROVIDERS",
    "PROVIDERS",
    "EmbeddingProvider",
    "LLMError",
    "LLMProvider",
    "LLMResult",
    "Message",
    "ProviderNotConfigured",
    "ToolCall",
    "ToolSpec",
    "get_embedder",
    "get_provider",
    "inline_refs",
]
