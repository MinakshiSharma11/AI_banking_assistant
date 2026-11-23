"""Data agent flows with a scripted fake LLM; database and OBDX calls are stubbed.

Covers: event order, the one retry, refusals (decline, blocked SQL, role gate),
clarification, grounding fallback, LLM failure, PII masking for hosted providers,
conversation memory and the audit record.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from aiplatform.audit.writer import AuditRecord
from aiplatform.gateway.auth import AuthContext
from aiplatform.llm.base import LLMError, LLMResult, Message, ToolCall, ToolSpec
from assistants.data.agent import agent as agent_mod
from assistants.data.agent.agent import ConversationContext, DataAgent
from assistants.data.agent.obdx_client import ApiResult, ObdxClient
from assistants.data.semantic.loader import load_semantic_model

TODAY = date(2026, 11, 17)
HO = AuthContext("ho_analyst", "HO", "head_office", ("001", "002"))
IT = AuthContext("it_ops", "IT", "it_support", ("001", "002"))
MODEL = load_semantic_model(tz="Asia/Kuwait")


class FakeLLM:
    def __init__(
        self, calls: list[ToolCall | None], answer: str | Exception = "", name: str = "ollama"
    ) -> None:
        self.calls = list(calls)
        self.answer = answer
        self.name = name
        self.model = "fake-1"
        self.prompts: list[str] = []
        self.tool_names: list[list[str]] = []

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        require_tool: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        self.prompts.append(
            messages[-1].content if len(messages) == 1 else "\n".join(m.content for m in messages)
        )
        self.tool_names.append([t.name for t in tools or []])
        return LLMResult(
            text=None,
            tool_call=self.calls.pop(0),
            model=self.model,
            input_tokens=100,
            output_tokens=10,
        )

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        self.prompts.append(messages[-1].content)
        if isinstance(self.answer, Exception):
            raise self.answer
        for word in self.answer.split(" "):
            yield word + " "


class FakeObdx:
    async def call(self, name: str, args: Any, auth: AuthContext) -> ApiResult:
        return ApiResult(
            ["active_sessions"],
            [[77]],
            "Active sessions right now.",
            {"active_sessions": 77, "by_channel": {"MOBILE": 60, "WEB": 17}},
        )


def make_agent(
    llm: FakeLLM, rows: list[list[Any]] | None = None, monkeypatch: pytest.MonkeyPatch | None = None
) -> tuple[DataAgent, list[AuditRecord], list[tuple[str, dict[str, Any]]]]:
    audits: list[AuditRecord] = []
    executed: list[tuple[str, dict[str, Any]]] = []
    a = DataAgent(
        llm,
        None,
        MODEL,
        obdx=cast(ObdxClient, FakeObdx()),
        audit=audits.append,
        today=lambda: TODAY,
    )

    async def fake_run(
        exec_sql: str, params: dict[str, Any], auth: AuthContext
    ) -> tuple[list[str], list[list[Any]], int, bool]:
        executed.append((exec_sql, params))
        data = rows if rows is not None else [["Salmiya", 291], ["Hawalli", 149]]
        cols = ["branch", "value"] if data and len(data[0]) == 2 else ["value"]
        return cols, data, len(data), False

    a._run_sql = fake_run  # type: ignore[method-assign]
    return a, audits, executed


@pytest.fixture(autouse=True)
def _no_db_branch_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_mod, "_branch_names", lambda: {"001": "Salmiya", "002": "KCM"})


async def run(
    a: DataAgent, q: str, auth: AuthContext = HO, ctx: ConversationContext | None = None
) -> list[dict[str, Any]]:
    return [ev async for ev in a.run(q, auth, ctx)]


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


METRIC = ToolCall(
    name="build_metric_query",
    args={
        "metric": "accounts_opened",
        "dimensions": ["branch"],
        "time": {"period": "last_quarter"},
    },
)


async def test_metric_path_event_order_and_audit() -> None:
    llm = FakeLLM([METRIC], answer="Last quarter Salmiya opened 291 accounts and Hawalli 149.")
    a, audits, executed = make_agent(llm)
    ev = await run(a, "accounts opened by branch last quarter")
    assert kinds(ev) == [
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
    assert ev[3]["rows"] == [["Salmiya", 291], ["Hawalli", 149]]
    assert ev[4] == {
        "type": "chart",
        "chart_type": "bar",
        "x": "branch",
        "y": "value",
        "series": None,
    }
    done = ev[-1]
    assert done["route"] == "metric" and done["status"] == "ok" and done["grounded"] is True
    assert executed[0][1]["user_branches"] == ["001", "002"]  # identity from AuthContext
    (rec,) = audits
    assert (rec.route, rec.status, rec.row_count, rec.user_role) == (
        "metric",
        "ok",
        2,
        "head_office",
    )
    assert rec.metric_query and rec.metric_query["metric"] == "accounts_opened"
    assert rec.sql_text and ":user_branches" in rec.sql_text
    assert (rec.llm_provider, rec.llm_model, rec.input_tokens) == ("ollama", "fake-1", 100)


async def test_one_retry_on_invalid_metric_query() -> None:
    bad = ToolCall(
        name="build_metric_query",
        args={"metric": "accounts_opened", "dimensions": ["colour"], "time": {"period": "ytd"}},
    )
    llm = FakeLLM([bad, METRIC], answer="Salmiya 291.")
    a, audits, _ = make_agent(llm)
    ev = await run(a, "accounts by colour this year")
    plans = [e for e in ev if e["type"] == "plan"]
    assert [p["attempt"] for p in plans] == [1, 2]
    assert "cannot be broken down by 'colour'" in llm.prompts[1]
    assert ev[-1]["status"] == "ok"


async def test_gives_up_after_one_retry() -> None:
    bad = ToolCall(name="build_metric_query", args={"metric": "nope"})
    a, audits, _ = make_agent(FakeLLM([bad, bad]))
    ev = await run(a, "something odd")
    assert [e["code"] for e in ev if e["type"] == "error"] == ["plan_failed"]
    assert audits[0].status == "error" and "planner failed" in (audits[0].error or "")


async def test_no_tool_call_is_retried() -> None:
    a, _, _ = make_agent(FakeLLM([None, METRIC], answer="Salmiya 291."))
    ev = await run(a, "accounts by branch last quarter")
    assert ev[-1]["status"] == "ok"


async def test_decline_is_refused_in_the_question_language() -> None:
    llm = FakeLLM([ToolCall(name="decline", args={"reason": "personal_data"})])
    a, audits, executed = make_agent(llm)
    ev = await run(a, "List customer names with balance above 10,000")
    assert "personal data" in ev[-2]["text"] and not executed
    assert (audits[0].route, audits[0].status) == ("refused", "ok")
    ev = await run(
        make_agent(FakeLLM([ToolCall(name="decline", args={"reason": "not_data_question"})]))[0],
        "ما هو الطقس اليوم؟",
    )
    assert "البيانات التشغيلية" in ev[-2]["text"]


@pytest.mark.parametrize(
    "sql,reason_text",
    [
        ("SELECT full_name FROM core.customer", "personal data"),
        ("SELECT * FROM audit.query_log", "isn't available"),
        ("DELETE FROM core.transaction", "only read"),
        ("SELECT pg_sleep(10)", "isn't allowed"),
    ],
)
async def test_blocked_sql_is_refused_without_retry(sql: str, reason_text: str) -> None:
    llm = FakeLLM([ToolCall(name="write_sql", args={"sql": sql, "reason": "x"})])
    a, audits, executed = make_agent(llm)
    ev = await run(a, "show me")
    assert reason_text in ev[-2]["text"] and not executed
    assert len(llm.prompts) == 1  # no retry for security rejections
    assert (audits[0].route, audits[0].status) == ("refused", "blocked")


async def test_fixable_sql_error_is_retried() -> None:
    llm = FakeLLM(
        [
            ToolCall(
                name="write_sql", args={"sql": "SELECT count(*) FROM cust_account", "reason": "x"}
            ),
            ToolCall(
                name="write_sql",
                args={"sql": "SELECT count(*) AS n FROM core.cust_account", "reason": "x"},
            ),
        ],
        answer="There are 2 rows.",
    )
    a, _, executed = make_agent(llm, rows=[[2]])
    ev = await run(a, "count accounts")
    assert "schema-qualified" in llm.prompts[1]
    assert "ANY(%(user_branches)s)" in executed[0][0]  # wrapper applied
    assert ev[-1]["route"] == "sql" and ev[-1]["status"] == "ok"


async def test_role_gate_it_support() -> None:
    llm = FakeLLM([METRIC])
    a, audits, executed = make_agent(llm)
    ev = await run(a, "accounts opened last quarter", auth=IT)
    assert "build_metric_query" in llm.tool_names[0]  # offered; the role is enforced in code
    assert "live operational data only" in ev[-2]["text"] and not executed
    assert audits[0].status == "blocked"


async def test_api_route_with_totals_as_evidence() -> None:
    llm = FakeLLM(
        [
            ToolCall(
                name="call_api", args={"name": "get_active_sessions", "args": {"by_channel": False}}
            )
        ],
        answer="77 users are active right now, 60 on mobile.",
    )
    a, _, _ = make_agent(llm)
    ev = await run(a, "how many users are logged in right now?", auth=IT)
    assert ev[-1]["route"] == "api" and ev[-1]["grounded"] is True
    assert next(e for e in ev if e["type"] == "chart")["chart_type"] == "kpi"
    assert "Totals from the API" in llm.prompts[-1]


async def test_api_args_are_validated() -> None:
    bad = ToolCall(
        name="call_api", args={"name": "get_session_summary", "args": {"minutes": 100000}}
    )
    good = ToolCall(name="call_api", args={"name": "get_active_sessions", "args": {}})
    llm = FakeLLM([bad, good], answer="77 active.")
    a, _, _ = make_agent(llm)
    ev = await run(a, "logins in the last 100000 minutes", auth=IT)
    assert "Invalid args for get_session_summary" in llm.prompts[1]
    assert ev[-1]["status"] == "ok"


async def test_clarification() -> None:
    llm = FakeLLM(
        [
            ToolCall(
                name="ask_clarification",
                args={
                    "question": "Which period?",
                    "options": ["This month", "Last month", "This year"],
                },
            )
        ]
    )
    a, audits, _ = make_agent(llm)
    ctx = ConversationContext()
    ev = await run(a, "How many accounts were opened?", ctx=ctx)
    assert kinds(ev) == ["status", "plan", "clarify", "done"]
    assert audits[0].route == "clarify" and ctx.turns[-1][1].startswith("Which period?")


async def test_grounding_failure_uses_template() -> None:
    llm = FakeLLM([METRIC], answer="Salmiya opened 9,999 accounts.")
    a, _, _ = make_agent(llm)
    ev = await run(a, "accounts by branch last quarter")
    text = next(e["text"] for e in ev if e["type"] == "answer_delta")
    assert "9,999" not in text and "Salmiya: 291" in text
    assert ev[-1]["grounded"] is False


async def test_llm_answer_failure_uses_template_and_planner_failure_is_reported() -> None:
    llm = FakeLLM([METRIC], answer=LLMError("down"))
    a, _, _ = make_agent(llm, rows=[[Decimal("291")]])
    ev = await run(a, "accounts by branch last quarter")
    assert ev[-1]["status"] == "ok" and ev[-1]["grounded"] is False
    assert "291" in next(e["text"] for e in ev if e["type"] == "answer_delta")

    class Down(FakeLLM):
        async def complete(self, *a: Any, **k: Any) -> LLMResult:
            raise LLMError("timeout")

    ev = await run(make_agent(Down([]))[0], "anything")
    assert [e["code"] for e in ev if e["type"] == "error"] == ["llm_unavailable"]


async def test_hosted_providers_get_masked_text() -> None:
    llm = FakeLLM([ToolCall(name="decline", args={"reason": "personal_data"})], name="anthropic")
    a, audits, _ = make_agent(llm)
    await run(a, "status of civil id 287654321012")
    assert "287654321012" not in llm.prompts[0] and "[CIVIL_ID]" in llm.prompts[0]
    assert audits[0].question.endswith("287654321012")  # audit keeps the original


async def test_follow_up_uses_previous_query() -> None:
    llm = FakeLLM(
        [
            METRIC,
            ToolCall(
                name="build_metric_query",
                args={**METRIC.args, "filters": [{"dimension": "branch", "values": ["Salmiya"]}]},
            ),
        ],
        answer="Salmiya 291.",
    )
    a, _, _ = make_agent(llm)
    ctx = ConversationContext()
    await run(a, "accounts opened by branch last quarter", ctx=ctx)
    assert ctx.last_metric_query and ctx.last_metric_query["metric"] == "accounts_opened"
    await run(a, "Now only for Salmiya", ctx=ctx)
    assert "PREVIOUS QUERY" in llm.prompts[2] and "accounts_opened" in llm.prompts[2]
    assert len(ctx.turns) == 4


async def test_bad_requests() -> None:
    a, audits, _ = make_agent(FakeLLM([]))
    for q in ("   ", "x" * 2001):
        ev = await run(a, q)
        assert ev[0]["code"] == "bad_request"
    assert len(audits) == 2 and all(r.status == "error" for r in audits)


async def test_audit_failure_does_not_break_the_answer() -> None:
    def broken(rec: AuditRecord) -> None:
        raise RuntimeError("db down")

    llm = FakeLLM([ToolCall(name="decline", args={"reason": "other"})])
    a = DataAgent(
        llm,
        None,
        MODEL,
        obdx=cast(ObdxClient, FakeObdx()),
        audit=broken,
        today=lambda: TODAY,
    )
    ev = await run(a, "hello")
    assert ev[-1]["type"] == "done"


async def test_period_guard_asks_instead_of_assuming() -> None:
    llm = FakeLLM(
        [
            ToolCall(
                name="build_metric_query",
                args={"metric": "accounts_opened", "time": {"period": "this_month"}},
            )
        ]
    )
    a, audits, executed = make_agent(llm)
    ev = await run(a, "How many accounts were opened?")
    clarify = next(e for e in ev if e["type"] == "clarify")
    assert clarify["question"] == "For which period?" and not executed
    assert audits[0].route == "clarify"
    llm2 = FakeLLM(
        [
            ToolCall(
                name="build_metric_query",
                args={"metric": "accounts_opened", "time": {"period": "this_month"}},
            )
        ]
    )
    ev2 = await run(make_agent(llm2)[0], "كم عدد الحسابات التي تم فتحها؟")
    assert next(e for e in ev2 if e["type"] == "clarify")["question"] == "لأي فترة؟"
    live = FakeLLM(
        [ToolCall(name="build_metric_query", args={"metric": "applications_in_stage"})],
        answer="12 cases.",
    )
    a3, _, executed3 = make_agent(live, rows=[[12]])
    ev3 = await run(a3, "loan cases by stage")  # live metric: no period needed
    assert ev3[-1]["route"] == "metric" and executed3


async def test_clarification_reply_completes_the_original_question() -> None:
    """Reported loop: 'accounts opened by branches' -> period options -> 'This month' must
    answer the original question, not start a new one."""
    no_period = ToolCall(
        name="build_metric_query",
        args={"metric": "accounts_opened", "dimensions": ["branch"]},
    )
    with_period = ToolCall(
        name="build_metric_query",
        args={
            "metric": "accounts_opened",
            "dimensions": ["branch"],
            "time": {"period": "this_month"},
        },
    )
    llm = FakeLLM([no_period, with_period], answer="Salmiya opened 291 and Hawalli 149.")
    a, audits, executed = make_agent(llm)
    ctx = ConversationContext()
    ev = await run(a, "give me accounts opened by branches", ctx=ctx)
    assert ev[-1]["route"] == "clarify" and ctx.pending_clarification is not None
    ev2 = await run(a, "This month", ctx=ctx)
    assert ev2[-1]["route"] == "metric" and executed
    assert "give me accounts opened by branches — This month" in llm.prompts[1]
    assert audits[1].question == "give me accounts opened by branches — This month"
    assert ctx.pending_clarification is None


async def test_chained_clarifications_keep_every_answer() -> None:
    ask_metric = ToolCall(
        name="ask_clarification",
        args={"question": "Which metric?", "options": ["Accounts opened", "Accounts closed"]},
    )
    no_period = ToolCall(name="build_metric_query", args={"metric": "accounts_opened"})
    with_period = ToolCall(
        name="build_metric_query",
        args={"metric": "accounts_opened", "time": {"period": "this_month"}},
    )
    llm = FakeLLM([ask_metric, no_period, with_period], answer="291 accounts.")
    a, _, executed = make_agent(llm, rows=[[291]])
    ctx = ConversationContext()
    await run(a, "accounts by branch", ctx=ctx)
    ev = await run(a, "Accounts opened", ctx=ctx)
    assert ev[-1]["route"] == "clarify"  # period guard
    ev = await run(a, "This month", ctx=ctx)
    assert ev[-1]["route"] == "metric" and executed
    assert "accounts by branch — Accounts opened — This month" in llm.prompts[2]


async def test_misspelt_branch_asks_did_you_mean_then_answers() -> None:
    """Reported: 'accounts opened by Hawaii last quarter' was refused with a role message."""
    typo = ToolCall(
        name="build_metric_query",
        args={
            "metric": "accounts_opened",
            "filters": [{"dimension": "branch", "op": "in", "values": ["Hawaii"]}],
            "time": {"period": "last_quarter"},
        },
    )
    fixed = ToolCall(
        name="build_metric_query",
        args={
            "metric": "accounts_opened",
            "filters": [{"dimension": "branch", "op": "in", "values": ["Hawalli"]}],
            "time": {"period": "last_quarter"},
        },
    )
    llm = FakeLLM([typo, fixed], answer="Hawalli opened 149 accounts.")
    a, audits, executed = make_agent(llm, rows=[[149]])
    ctx = ConversationContext()
    ev = await run(a, "accounts opened by Hawaii last quarter", ctx=ctx)
    clarify = next(e for e in ev if e["type"] == "clarify")
    assert "no branch called 'Hawaii'" in clarify["question"] and not executed
    assert ev[-1]["route"] == "clarify" and len(llm.prompts) == 1  # no planner retry
    ev2 = await run(a, "Salmiya", ctx=ctx)  # any of the user's branches may be chosen
    assert ev2[-1]["route"] == "metric" and executed
    assert "accounts opened by Salmiya last quarter" in llm.prompts[1]


async def test_name_mangled_by_the_planner_is_retried_not_asked() -> None:
    bad = ToolCall(
        name="build_metric_query",
        args={
            "metric": "accounts_opened",
            "filters": [{"dimension": "branch", "op": "in", "values": ["Kuwait City"]}],
            "time": {"period": "last_quarter"},
        },
    )
    good = ToolCall(
        name="build_metric_query",
        args={
            "metric": "accounts_opened",
            "filters": [{"dimension": "branch", "op": "in", "values": ["Kuwait City Main"]}],
            "time": {"period": "last_quarter"},
        },
    )
    llm = FakeLLM([bad, good], answer="291 accounts.")
    a, _, executed = make_agent(llm, rows=[[291]])
    ev = await run(a, "accounts opened at KCM branch last quarter")
    assert ev[-1]["route"] == "metric" and executed
    assert "not a valid branch" in llm.prompts[1]


async def test_model_cannot_claim_a_role_refusal() -> None:
    llm = FakeLLM(
        [
            ToolCall(name="decline", args={"reason": "role_not_permitted"}),
            ToolCall(name="decline", args={"reason": "role_not_permitted"}),
        ]
    )
    a, audits, _ = make_agent(llm)
    ev = await run(a, "accounts opened by Hawaii last quarter")
    assert not any("Your role" in str(e.get("message", "")) for e in ev)
    # 'role_not_permitted' is not a valid decline reason any more: the plan fails instead
    assert ev[-2]["code"] == "plan_failed" and "Extra" not in (audits[0].error or "")


async def test_decline_with_misspelt_branch_becomes_did_you_mean() -> None:
    llm = FakeLLM([ToolCall(name="decline", args={"reason": "other"})])
    a, audits, _ = make_agent(llm)
    a_ctx = ConversationContext()
    ev = await run(a, "accounts opened by Salmiyah last quarter", ctx=a_ctx)
    clarify = next(e for e in ev if e["type"] == "clarify")
    assert clarify["options"] == ["Salmiya"] and audits[0].route == "clarify"
    assert a_ctx.pending_clarification is not None
    assert a_ctx.pending_clarification.replace == "Salmiyah"


async def test_personal_data_refusal_is_never_overridden() -> None:
    llm = FakeLLM([ToolCall(name="decline", args={"reason": "personal_data"})])
    a, audits, _ = make_agent(llm)
    ev = await run(a, "phone numbers of customers in Salmiyah")
    assert ev[-1]["route"] == "refused" and not any(e["type"] == "clarify" for e in ev)


def test_series_totals_per_channel() -> None:
    from assistants.data.agent.agent import series_totals

    rows = [
        ["2026-01-01", "Mobile", Decimal("10.5")],
        ["2026-01-01", "Web", Decimal("4")],
        ["2026-02-01", "Mobile", Decimal("2.25")],
    ]
    assert series_totals(["period", "channel", "value"], rows) == [
        ["Mobile", Decimal("12.75")],
        ["Web", 4],
    ]
    assert series_totals(["period", "value"], [["2026-01-01", 3], ["2026-02-01", 4]]) == [[7]]
    assert series_totals(["channel", "value"], [["Mobile", 3]]) == []  # not a trend


async def test_total_follow_up_drops_monthly_breakdown() -> None:
    """Reported: 'give me the total value for web and mobile' after a monthly question
    returned months again. The follow-up keeps the period but loses the grain."""
    monthly = {
        "metric": "txn_value_kwd",
        "dimensions": ["channel"],
        "time": {"period": "last_12_months"},
        "grain": "month",
    }
    llm = FakeLLM(
        [ToolCall(name="build_metric_query", args=monthly)] * 2,
        answer="Mobile 12.75 KWD and Web 4 KWD.",
    )
    a, audits, _ = make_agent(llm)
    trend = [["2026-01-01", "Mobile", Decimal("10.5")], ["2026-02-01", "Web", 4]]

    async def fake_run(
        exec_sql: str, params: dict[str, Any], auth: AuthContext
    ) -> tuple[list[str], list[list[Any]], int, bool]:
        return ["period", "channel", "value"], trend, len(trend), False

    a._run_sql = fake_run  # type: ignore[method-assign]
    ctx = ConversationContext()
    ev = await run(a, "Mobile vs web transaction value by month", ctx=ctx)
    result = next(e for e in ev if e["type"] == "result")
    assert result["totals"] and result["totals_columns"] == ["channel", "value"]
    await run(a, "give me the total value for web and mobile", ctx=ctx)
    assert "grain" not in (audits[1].metric_query or {})
    assert audits[1].metric_query["time"] == {"period": "last_12_months"}  # type: ignore[index]


async def test_follow_up_without_time_words_keeps_the_period() -> None:
    first = ToolCall(
        name="build_metric_query",
        args={"metric": "accounts_opened", "time": {"period": "last_quarter"}},
    )
    drifted = ToolCall(
        name="build_metric_query",
        args={
            "metric": "accounts_opened",
            "filters": [{"dimension": "branch", "op": "in", "values": ["Salmiya"]}],
            "time": {"period": "today"},
        },
    )
    llm = FakeLLM([first, drifted], answer="291 accounts.")
    a, audits, _ = make_agent(llm, rows=[[291]])
    ctx = ConversationContext()
    await run(a, "accounts opened last quarter", ctx=ctx)
    await run(a, "Now only for Salmiya", ctx=ctx)
    assert audits[1].metric_query["time"] == {"period": "last_quarter"}  # type: ignore[index]
    llm2 = FakeLLM([first, drifted], answer="12 accounts.")
    a2, audits2, _ = make_agent(llm2, rows=[[12]])
    ctx2 = ConversationContext()
    await run(a2, "accounts opened last quarter", ctx=ctx2)
    await run(a2, "same for Salmiya today", ctx=ctx2)  # a new period was asked for
    assert audits2[1].metric_query["time"] == {"period": "today"}  # type: ignore[index]


async def test_sql_event_and_audit_carry_the_values_used() -> None:
    llm = FakeLLM([METRIC], answer="Salmiya opened 291 accounts and Hawalli 149.")
    a, audits, executed = make_agent(llm)
    ev = await run(a, "accounts opened by branch last quarter")
    sql = next(e for e in ev if e["type"] == "sql")
    assert ":t_from" in sql["text"]  # what ran keeps bind parameters
    assert sql["params"]["user_branches"] == ["001", "002"]
    assert set(sql["params"]) >= {"t_from", "t_to", "user_branches"}
    assert ":t_from" not in sql["text_with_values"]
    assert "ARRAY['001', '002']" in sql["text_with_values"]
    assert sql["params"]["t_from"] in sql["text_with_values"].replace(" ", "T", 1) or (
        str(sql["params"]["t_from"])[:10] in sql["text_with_values"]
    )
    assert "-- values:" in (audits[0].sql_text or "") and '"001"' in (audits[0].sql_text or "")
    assert executed[0][1]["user_branches"] == ["001", "002"]  # execution unchanged


def test_sql_with_values_quotes_and_skips_casts() -> None:
    from assistants.data.agent.agent import sql_with_values

    out = sql_with_values(
        "SELECT a::date FROM t WHERE n = :name AND m = :name_2 AND d >= :d",
        {"name": "O'Neil", "name_2": 5, "d": date(2026, 7, 1)},
    )
    assert out == ("SELECT a::date FROM t WHERE n = 'O''Neil' AND m = 5 AND d >= DATE '2026-07-01'")
