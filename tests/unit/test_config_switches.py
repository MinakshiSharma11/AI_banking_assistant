"""Configuration switches for real deployments: keys for OpenAI-compatible servers, a
separate embedding endpoint, OBDX service credentials, and the seed guard."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aiplatform.llm import config as llm_config
from aiplatform.llm import get_embedder, get_provider
from assistants.data.agent.obdx_client import ObdxClient
from mocks.seed import ensure, run


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    llm_config.get_llm_settings.cache_clear()
    yield
    llm_config.get_llm_settings.cache_clear()


def test_openai_compatible_key_and_embedding_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://vllm.bank.local/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret-1")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embed.bank.local/v1")
    monkeypatch.setenv("EMBED_MODEL", "bge-m3")
    p = get_provider()
    assert p.client.api_key == "secret-1"  # type: ignore[attr-defined]
    e = get_embedder()
    assert e is not None and str(e.client.base_url).startswith("https://embed.bank.local")  # type: ignore[attr-defined]
    assert e.client.api_key == "secret-1"  # type: ignore[attr-defined]


def test_empty_embed_model_disables_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_MODEL", "")
    assert get_embedder() is None


def test_obdx_client_sends_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBDX_API_TOKEN", "svc-token")
    monkeypatch.setenv("OBDX_TIMEOUT_S", "7")
    c = ObdxClient(base_url="https://obdx.bank.local")
    client = c._client()
    assert client.headers["Authorization"] == "Bearer svc-token"
    assert c.timeout_s == 7


def test_seed_refuses_when_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED_ALLOWED", "false")
    assert run.main() == 2
    assert ensure.main() == 0  # the API container starts without touching the database
