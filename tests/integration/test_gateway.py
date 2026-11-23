"""Gateway (spec section 2) against the seeded DB with a scripted LLM."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from aiplatform.audit.writer import AuditRecord
from aiplatform.gateway.app import create_app, sse
from aiplatform.gateway.security import RateLimiter, gateway_settings
from aiplatform.llm.base import LLMResult, Message, ToolCall, ToolSpec
from assistants.data.agent.agent import DataAgent
from assistants.data.settings import get_settings

pytestmark = pytest.mark.db

METRIC = ToolCall(
    name="build_metric_query",
    args={"metric": "accounts_opened", "dimensions": ["branch"], "time": {"period": "ytd"}},
)


class ScriptedLLM:
    name, model = "ollama", "scripted"

    def __init__(self) -> None:
        self.queue: list[ToolCall] = []
        self.prompts: list[str] = []

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        require_tool: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        self.prompts.append(messages[-1].content)
        return LLMResult(text=None, tool_call=self.queue.pop(0), model=self.model)

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        yield "Here are the accounts opened."


@pytest.fixture(scope="module", autouse=True)
def _db_up() -> None:
    try:
        psycopg.connect(get_settings().agent_url, connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.skip("seeded database not reachable; run make up seed")


@pytest.fixture
def env() -> Any:
    llm = ScriptedLLM()
    audits: list[AuditRecord] = []
    feedback: list[dict[str, Any]] = []
    app = create_app(
        agent_factory=lambda: DataAgent(llm, None, audit=audits.append),
        audit=audits.append,
        feedback_writer=feedback.append,
        run_decoder_loop=False,
    )
    return TestClient(app), llm, audits, feedback


def login(client: TestClient, user: str) -> dict[str, str]:
    r = client.post("/auth/login", json={"username": user})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def events(resp: Any) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in resp.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_login_me_and_token_checks(env: Any) -> None:
    client, *_ = env
    assert {u["username"] for u in client.get("/auth/users").json()} == {
        "bm_salmiya",
        "rh_north",
        "ho_analyst",
        "it_ops",
    }
    h = login(client, "rh_north")
    me = client.get("/me", headers=h).json()
    assert me["role"] == "regional_head" and me["branches"] == ["005", "006", "007", "008"]
    assert client.post("/auth/login", json={"username": "mallory"}).status_code == 401
    assert client.get("/me").status_code == 401
    forged = jwt.encode(
        {
            "sub": "x",
            "role": "head_office",
            "branches": ["001"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "iss": "bcai-mock-sso",
        },
        "attacker-secret-0000",
        algorithm="HS256",
    )
    assert client.get("/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = jwt.encode(
        {
            "sub": "x",
            "role": "head_office",
            "branches": ["001"],
            "iat": 1,
            "exp": 2,
            "iss": "bcai-mock-sso",
        },
        gateway_settings().jwt_secret,
        algorithm="HS256",
    )
    assert client.get("/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_chat_streams_the_sse_contract(env: Any) -> None:
    client, llm, audits, _ = env
    llm.queue.append(METRIC)
    h = login(client, "bm_salmiya")
    r = client.post("/chat", json={"message": "accounts opened by branch this year"}, headers=h)
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = events(r)
    assert [k for k, _ in ev] == [
        "conversation",
        "status",
        "plan",
        "status",
        "result",
        "chart",
        "status",
        "answer_delta",
        "definition",
        "sql",
        "done",
    ]
    data = dict(ev)
    assert data["chart"]["type"] == "kpi" or data["chart"]["type"] in ("bar", "table")
    assert [row[0] for row in data["result"]["rows"]] == ["Salmiya"]  # entitlement from JWT
    assert data["result"]["unit"] == "accounts"
    assert data["done"]["route"] == "metric" and audits[-1].username == "bm_salmiya"


def test_body_cannot_override_identity(env: Any) -> None:
    client, llm, _, _ = env
    h = login(client, "bm_salmiya")
    r = client.post("/chat", json={"message": "x", "username": "ho_analyst"}, headers=h)
    assert r.status_code == 422  # extra fields are rejected


def test_conversations_are_owned_and_follow_ups_get_context(env: Any) -> None:
    client, llm, _, _ = env
    llm.queue += [
        METRIC,
        ToolCall(
            name="build_metric_query",
            args={**METRIC.args, "filters": [{"dimension": "branch", "values": ["Jahra"]}]},
        ),
    ]
    h = login(client, "rh_north")
    ev = events(client.post("/chat", json={"message": "accounts by branch this year"}, headers=h))
    cid = dict(ev)["conversation"]["conversation_id"]
    events(client.post("/chat", json={"conversation_id": cid, "message": "only Jahra"}, headers=h))
    assert "PREVIOUS QUERY" in llm.prompts[-1] and "accounts_opened" in llm.prompts[-1]
    hist = client.get(f"/conversations/{cid}", headers=h).json()
    assert [m["question"] for m in hist["messages"]] == [
        "accounts by branch this year",
        "only Jahra",
    ]
    other = login(client, "ho_analyst")
    assert client.get(f"/conversations/{cid}", headers=other).status_code == 404
    llm.queue.append(METRIC)
    ev2 = events(
        client.post("/chat", json={"conversation_id": cid, "message": "hi"}, headers=other)
    )
    assert dict(ev2)["conversation"]["conversation_id"] != cid  # cannot join someone else's


def test_message_cap_and_rate_limit_are_enforced_and_audited(env: Any) -> None:
    client, llm, audits, _ = env
    h = login(client, "ho_analyst")
    r = client.post("/chat", json={"message": "x" * 2001}, headers=h)
    assert r.status_code == 413 and audits[-1].error == "message too long"
    limit = gateway_settings().rate_per_min
    for _ in range(limit):
        llm.queue.append(ToolCall(name="decline", args={"reason": "other"}))
        assert client.post("/chat", json={"message": "hello"}, headers=h).status_code == 200
    r = client.post("/chat", json={"message": "hello"}, headers=h)
    assert r.status_code == 429 and audits[-1].status == "blocked"


def test_feedback_marks_candidate_examples(env: Any) -> None:
    client, llm, _, feedback = env
    llm.queue.append(METRIC)
    h = login(client, "ho_analyst")
    done = dict(
        events(client.post("/chat", json={"message": "accounts by branch this year"}, headers=h))
    )["done"]
    r = client.post("/feedback", json={"message_id": done["message_id"], "rating": 1}, headers=h)
    assert r.json() == {"ok": True, "candidate_example": True}
    assert json.loads(feedback[-1]["metric_query"])["metric"] == "accounts_opened"
    other = login(client, "it_ops")
    assert (
        client.post(
            "/feedback", json={"message_id": done["message_id"], "rating": -1}, headers=other
        ).status_code
        == 404
    )


def test_feedback_is_persisted_with_the_gateway_role() -> None:
    from aiplatform.gateway.app import _write_feedback

    _write_feedback(
        {
            "message_id": "00000000-0000-0000-0000-000000000001",
            "username": "test",
            "rating": -1,
            "comment": "wrong period",
            "question": "q",
            "route": "metric",
            "metric_query": None,
            "candidate_example": False,
        }
    )
    with psycopg.connect(get_settings().decoder_url) as c:
        row = c.execute(
            "SELECT rating, comment FROM app.feedback WHERE username = 'test' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == (-1, "wrong period")


def test_health(env: Any) -> None:
    client, *_ = env
    body = client.get("/health").json()
    assert body["db"]["ok"] is True and body["db"]["decoder"]["backlog"] == 0
    assert body["llm"]["provider"]


def test_sse_frame_and_rate_limiter_window() -> None:
    assert (
        sse({"type": "chart", "chart_type": "bar", "x": "b"})
        == 'event: chart\ndata: {"x": "b", "type": "bar"}\n\n'
    )
    rl = RateLimiter(2)
    assert rl.allow("u", 0) and rl.allow("u", 1) and not rl.allow("u", 2)
    assert rl.allow("u", 61)  # window slides


def test_export_excel_and_pdf_are_owner_only_and_audited(env: Any) -> None:
    import io

    from openpyxl import load_workbook

    client, llm, audits, _ = env
    llm.queue.append(METRIC)
    h = login(client, "ho_analyst")
    evs = dict(
        events(client.post("/chat", json={"message": "accounts by branch this year"}, headers=h))
    )
    mid = evs["done"]["message_id"]
    shown = evs["result"]["rows"]

    r = client.get(f"/export/{mid}?format=xlsx", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(r.content))["Data"]
    data = [list(row) for row in ws.iter_rows(min_row=2, values_only=True)]
    assert len(data) == len(shown) and data[0][0] == shown[0][0]  # exactly what was shown
    about = load_workbook(io.BytesIO(r.content))["About"]
    assert about["B1"].value == "accounts by branch this year"

    r = client.get(f"/export/{mid}?format=pdf", headers=h)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")

    exports = [a for a in audits if a.route == "export"]
    assert len(exports) == 2 and exports[0].row_count == len(shown)
    assert exports[0].question.startswith("[export xlsx]") and exports[0].conversation_id

    other = login(client, "it_ops")
    assert client.get(f"/export/{mid}?format=xlsx", headers=other).status_code == 404
    assert client.get(f"/export/{mid}?format=csv", headers=h).status_code == 422
    assert client.get(f"/export/{mid}").status_code == 401


def test_handbook_page_and_downloads(env: Any) -> None:
    client, _, _, _ = env
    assert client.get("/handbook").status_code == 401
    h = login(client, "bm_salmiya")
    page = client.get("/handbook", headers=h).json()
    assert page["markdown"].count("data:image/png;base64,") == 3  # diagrams inlined
    assert "## 17. Connecting to the real FLEXCUBE, OBDX and Atumverse systems" in page["markdown"]
    md = client.get("/handbook/download?format=md", headers=h)
    assert md.status_code == 200 and "handbook.md" in md.headers["content-disposition"]
    html = client.get("/handbook/download?format=html", headers=h)
    assert html.status_code == 200 and html.text.startswith("<!doctype html>")
    assert "<table>" in html.text and 'id="6-installation"' in html.text
    assert client.get("/handbook/download?format=pdf", headers=h).status_code == 422
