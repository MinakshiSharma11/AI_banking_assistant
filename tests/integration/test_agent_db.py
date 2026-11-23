"""Agent end to end against the seeded DB and the mock OBDX API, with a scripted LLM.
Also: OBDX API contract and entitlements, and the gateway role's audit permissions."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from aiplatform.audit.writer import AuditRecord, write_audit
from aiplatform.gateway.auth import UnknownUser, gateway_db_url, load_staff_user
from aiplatform.llm.base import LLMResult, Message, ToolCall, ToolSpec
from assistants.data.agent.agent import ConversationContext, DataAgent
from assistants.data.settings import get_settings
from mocks.obdx.app import app
from mocks.seed.refresh_live import main as refresh_live

pytestmark = pytest.mark.db


@pytest.fixture(scope="module", autouse=True)
def _db_up() -> None:
    try:
        psycopg.connect(get_settings().agent_url, connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.skip("seeded database not reachable; run make up seed")


class ScriptedLLM:
    name, model = "ollama", "scripted"

    def __init__(self, *calls: ToolCall, answer: str = "") -> None:
        self.calls, self.answer = list(calls), answer

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        require_tool: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        return LLMResult(text=None, tool_call=self.calls.pop(0), model=self.model)

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        yield self.answer


def admin() -> psycopg.Connection[Any]:
    return psycopg.connect(get_settings().decoder_url)


def audit_row(message_id: str) -> tuple[Any, ...] | None:
    with admin() as c:
        return c.execute(
            "SELECT route, status, username, row_count, sql_text IS NOT NULL "
            "FROM audit.query_log WHERE message_id = %s",
            (message_id,),
        ).fetchone()


async def ask(llm: ScriptedLLM, q: str, user: str) -> list[dict[str, Any]]:
    agent = DataAgent(llm, None)
    return [e async for e in agent.run(q, load_staff_user(user), ConversationContext())]


async def test_metric_path_as_salmiya_manager_is_branch_scoped_and_audited() -> None:
    llm = ScriptedLLM(
        ToolCall(
            name="build_metric_query",
            args={"metric": "accounts_opened", "dimensions": ["branch"], "time": {"period": "ytd"}},
        )
    )
    ev = await ask(llm, "Accounts opened in all branches this year", "bm_salmiya")
    result = next(e for e in ev if e["type"] == "result")
    assert [r[0] for r in result["rows"]] == ["Salmiya"]
    done = ev[-1]
    assert audit_row(done["message_id"]) == ("metric", "ok", "bm_salmiya", 1, True)


async def test_sql_fallback_runs_wrapped_and_refusal_is_audited_as_blocked() -> None:
    llm = ScriptedLLM(
        ToolCall(
            name="write_sql",
            args={
                "sql": "SELECT branch_code, count(*) AS n FROM core.cust_account GROUP BY 1",
                "reason": "test",
            },
        )
    )
    ev = await ask(llm, "accounts per branch code", "rh_north")
    result = next(e for e in ev if e["type"] == "result")
    assert sorted(r[0] for r in result["rows"]) == ["005", "006", "007", "008"]
    llm2 = ScriptedLLM(
        ToolCall(
            name="write_sql", args={"sql": "SELECT full_name FROM core.customer", "reason": "x"}
        )
    )
    ev2 = await ask(llm2, "List customer names", "ho_analyst")
    assert audit_row(ev2[-1]["message_id"])[:2] == ("refused", "blocked")  # type: ignore[index]


async def test_api_path_through_mock_obdx() -> None:
    assert refresh_live(["--count", "60"]) == 0
    llm = ScriptedLLM(ToolCall(name="call_api", args={"name": "get_active_sessions", "args": {}}))
    ev = await ask(llm, "users logged in now", "it_ops")
    result = next(e for e in ev if e["type"] == "result")
    assert result["columns"] == ["active_sessions"] and result["rows"][0][0] >= 60
    assert next(e for e in ev if e["type"] == "chart")["chart_type"] == "kpi"


def test_obdx_api_contract_and_entitlements() -> None:
    client = TestClient(app)
    assert client.get("/digx/v1/admin/sessions/active").status_code == 422  # no header
    assert (
        client.get(
            "/digx/v1/admin/sessions/active", headers={"X-Entitled-Branches": "1; DROP"}
        ).status_code
        == 403
    )
    all_b = {"X-Entitled-Branches": ",".join(f"{i:03d}" for i in range(1, 13))}
    d = client.get("/digx/v1/admin/sessions/active", headers=all_b).json()
    assert d["active_sessions"] == sum(d["by_channel"].values())
    s = client.get("/digx/v1/admin/sessions/summary", params={"minutes": 60}, headers=all_b)
    assert s.status_code == 200 and s.json()["window_minutes"] == 60
    assert (
        client.get(
            "/digx/v1/admin/sessions/summary", params={"minutes": 1}, headers=all_b
        ).status_code
        == 422
    )
    with admin() as c:
        row = c.execute(
            "SELECT u.user_id, cu.home_branch FROM obdx.digx_user u "
            "JOIN core.customer cu USING (customer_no) "
            "WHERE cu.home_branch <> '001' LIMIT 1"
        ).fetchone()
    assert row is not None
    uid, home = row
    ok = client.get(f"/digx/v1/admin/users/{uid}/status", headers={"X-Entitled-Branches": home})
    assert ok.status_code == 200 and ok.json()["user_id"] == uid
    hidden = client.get(
        f"/digx/v1/admin/users/{uid}/status", headers={"X-Entitled-Branches": "001"}
    )
    assert hidden.status_code == 404  # other branch: indistinguishable from missing


def test_gateway_role_can_append_audit_but_not_read_or_touch_bank_data() -> None:
    mid = uuid.uuid4()
    write_audit(
        AuditRecord(
            username="test", question="q", route="refused", status="blocked", message_id=mid
        )
    )
    assert audit_row(str(mid)) == ("refused", "blocked", "test", None, False)
    with psycopg.connect(gateway_db_url()) as c:
        for q in (
            "SELECT * FROM audit.query_log",
            "SELECT * FROM core.cust_account",
            "DELETE FROM audit.query_log",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(q)
            c.rollback()
    assert load_staff_user("rh_north").branches == ("005", "006", "007", "008")
    with pytest.raises(UnknownUser):
        load_staff_user("nobody")
