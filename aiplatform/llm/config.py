"""LLM gateway configuration (env / .env). Switching provider is a config change only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    timeout_s: float
    ollama_base_url: str
    ollama_model: str
    ollama_think: bool
    ollama_timeout_s: float
    ollama_api_key: str  # OpenAI-compatible servers that need a key (vLLM, gateways)
    embed_base_url: str
    embed_api_key: str
    embed_model: str  # empty = no embeddings (example retrieval by word overlap)
    embed_query_instruction: str
    anthropic_api_key: str
    anthropic_model: str
    gemini_api_key: str
    gemini_model: str
    gemini_api_version: str  # empty = SDK default; preview models need v1beta
    cohere_api_key: str
    cohere_model: str


@cache
def get_llm_settings() -> LLMSettings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get
    return LLMSettings(
        provider=env("LLM_PROVIDER", "ollama").strip().lower(),
        timeout_s=float(env("LLM_TIMEOUT_S", "30")),
        ollama_base_url=env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        ollama_model=env("OLLAMA_MODEL", "qwen3:8b"),
        ollama_think=env("OLLAMA_THINK", "false").strip().lower() in ("1", "true", "yes"),
        # A local model's first call includes loading it into memory.
        ollama_timeout_s=float(env("OLLAMA_TIMEOUT_S", "120")),
        ollama_api_key=env("OLLAMA_API_KEY", "") or "ollama",
        embed_base_url=env("EMBED_BASE_URL", "")
        or env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        embed_api_key=env("EMBED_API_KEY", "") or env("OLLAMA_API_KEY", "") or "ollama",
        embed_model=env("EMBED_MODEL", "qwen3-embedding:0.6b").strip(),
        embed_query_instruction=env(
            "EMBED_QUERY_INSTRUCTION",
            "Instruct: Given a banking analytics question, retrieve similar questions\nQuery: ",
        ).replace("\\n", "\n"),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        anthropic_model=env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        gemini_api_key=env("GEMINI_API_KEY", ""),
        gemini_model=env("GEMINI_MODEL", ""),
        gemini_api_version=env("GEMINI_API_VERSION", "").strip(),
        cohere_api_key=env("COHERE_API_KEY", ""),
        cohere_model=env("COHERE_MODEL", ""),
    )
