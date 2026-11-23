"""Real local LLM (Ollama) on a few spec scenarios. Slow: excluded from `make test`, run
with `make test-live`. Accuracy is measured properly by the phase-5 golden set; these
check that the wiring works with a real model and that security never depends on it."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aiplatform.gateway.auth import load_staff_user
from aiplatform.llm import get_embedder, get_provider
from assistants.data.agent.agent import ConversationContext, DataAgent

pytestmark = [pytest.mark.live_llm, pytest.mark.db]


@pytest.fixture(scope="module", autouse=True)
def _ollama_up() -> None:
    try:
        httpx.get("http://localhost:11434/api/version", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Ollama not running")


async def ask(q: str, user: str = "ho_analyst") -> list[dict[str, Any]]:
    agent = DataAgent(get_provider("ollama"), get_embedder())
    return [e async for e in agent.run(q, load_staff_user(user), ConversationContext())]


def plan_args(ev: list[dict[str, Any]]) -> Any:
    return [e for e in ev if e["type"] == "plan"][-1]["args"]


async def test_metric_question() -> None:
    ev = await ask("How many accounts were opened last quarter by branch?")
    a = plan_args(ev)
    assert a["metric"] == "accounts_opened" and a["dimensions"] == ["branch"]
    assert a["time"]["period"] == "last_quarter" and ev[-1]["status"] == "ok"


async def test_arabic_question() -> None:
    ev = await ask("كم عدد الحسابات الجديدة هذا الشهر؟")
    a = plan_args(ev)
    assert a["metric"] == "accounts_opened" and a["time"]["period"] == "this_month"


async def test_pii_request_never_returns_names() -> None:
    ev = await ask("List customer names with balance above 10,000")
    assert ev[-1]["route"] == "refused"
    assert not any(e["type"] == "result" for e in ev)
