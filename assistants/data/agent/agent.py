"""The data agent: plan (LLM call 1) -> compile/validate/execute in code -> answer (LLM
call 2) -> grounding check. Emits the SSE event contract of spec section 2 so the phase-4
gateway can stream it unchanged:

    status, plan, clarify, result, chart, answer_delta, definition, sql, done | error

Identity and entitlements come only from AuthContext. One audit row per request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from typing import Any

import psycopg
from pydantic import BaseModel, ValidationError

from aiplatform.audit.writer import AuditRecord, write_audit
from aiplatform.gateway.auth import AuthContext
from aiplatform.guardrails.grounding import is_grounded
from aiplatform.guardrails.pii import mask_pii
from aiplatform.llm import LOCAL_PROVIDERS, EmbeddingProvider, LLMError, LLMProvider, Message
from aiplatform.tools.registry import ToolError, ToolForbidden
from assistants.data.agent import prompts
from assistants.data.agent.charts import choose_chart
from assistants.data.agent.examples_index import ExampleIndex
from assistants.data.agent.guards import (
    PendingClarification,
    answer_clarification,
    mentions_time,
    names_period,
    near_miss,
    period_clarification,
    trim_extras,
    unknown_value_clarification,
)
from assistants.data.agent.obdx_client import ApiNotFound, ObdxClient
from assistants.data.agent.tools import (
    API_ARGS,
    ROUTES,
    CallApiArgs,
    ClarifyArgs,
    DeclineArgs,
    WriteSqlArgs,
    build_registry,
)
from assistants.data.db import run_readonly
from assistants.data.semantic.compiler import SemanticError, UnknownValue, compile_query
from assistants.data.semantic.loader import SemanticModel, get_model
from assistants.data.semantic.query import MetricQuery
from assistants.data.semantic.validator import SECURITY_CODES, SQLRejected, validate_sql
from assistants.data.settings import get_settings

log = logging.getLogger(__name__)
Event = dict[str, Any]
MAX_QUESTION_CHARS = 2000
MAX_TURNS = 6
_ARABIC = re.compile(r"[؀-ۿ]")

REFUSALS: dict[str, tuple[str, str]] = {
    "personal_data": (
        "I can't share customers' personal data such as names, civil IDs, phone numbers or "
        "salaries. I can give counts, totals or breakdowns instead.",
        "لا يمكنني مشاركة البيانات الشخصية للعملاء مثل الأسماء أو الأرقام المدنية أو أرقام "
        "الهواتف أو الرواتب. يمكنني تقديم أعداد أو مجاميع أو تفصيلات بدلاً من ذلك.",
    ),
    "raw_documents": (
        "I can't read document contents or raw application data. I can tell you about "
        "document metadata instead: type, count, size and upload date.",
        "لا يمكنني قراءة محتوى المستندات أو بيانات الطلبات الخام. يمكنني تزويدك ببيانات "
        "وصفية عن المستندات بدلاً من ذلك: النوع والعدد والحجم وتاريخ الرفع.",
    ),
    "blocked_column": (
        "I can't show personal data, raw application payloads or document contents. I can "
        "give counts, totals, breakdowns or document metadata instead.",
        "لا يمكنني عرض البيانات الشخصية أو بيانات الطلبات الخام أو محتوى المستندات. يمكنني "
        "تقديم أعداد أو مجاميع أو تفصيلات أو بيانات وصفية للمستندات بدلاً من ذلك.",
    ),
    "blocked_table": (
        "That data isn't available to this assistant.",
        "هذه البيانات غير متاحة لهذا المساعد.",
    ),
    "write": (
        "I can only read data; I can't change anything.",
        "يمكنني قراءة البيانات فقط، ولا يمكنني تغيير أي شيء.",
    ),
    "not_allowed": ("That kind of query isn't allowed.", "هذا النوع من الاستعلامات غير مسموح."),
    "role": (
        "Your role can use live operational data only, for example active sessions or a "
        "digital user's status.",
        "صلاحيتك تقتصر على البيانات التشغيلية المباشرة، مثل الجلسات النشطة أو حالة مستخدم رقمي.",
    ),
    "not_data_question": (
        "I can only answer questions about the bank's operational data, such as accounts, "
        "transactions, digital banking and applications.",
        "يمكنني فقط الإجابة عن أسئلة حول البيانات التشغيلية للبنك، مثل الحسابات والمعاملات "
        "والخدمات الرقمية والطلبات.",
    ),
}
REFUSALS["other"] = REFUSALS["not_data_question"]
REFUSALS["role_not_permitted"] = REFUSALS["role"]
_SQL_REFUSAL = {
    "blocked_column": "blocked_column",
    "blocked_table": "blocked_table",
    "not_select": "write",
    "multi_statement": "write",
}


@dataclass
class ConversationContext:
    conversation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    turns: list[tuple[str, str]] = field(default_factory=list)
    last_metric_query: dict[str, Any] | None = None
    # set while a clarification is awaiting the user's reply
    pending_clarification: PendingClarification | None = None

    def remember(self, question: str, answer: str, metric_query: dict[str, Any] | None) -> None:
        self.turns = [*self.turns, ("user", question), ("assistant", answer)][-2 * MAX_TURNS :]
        if metric_query is not None:
            self.last_metric_query = metric_query


@dataclass
class _Outcome:
    route: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    definition: str = ""
    sql: str | None = None
    metric_query: dict[str, Any] | None = None
    unit: str = ""
    label: str = ""
    clarify: ClarifyArgs | None = None
    refusal: str | None = None
    totals: list[list[Any]] = field(default_factory=list)  # per-series totals of a trend
    params: dict[str, Any] = field(default_factory=dict)  # bind values the SQL ran with
    status: str = "ok"
    evidence: list[Any] = field(default_factory=list)  # extra numbers the answer may cite
    facts: dict[str, Any] = field(default_factory=dict)  # shown to the answerer (API totals)


class _Retry(Exception):
    """Planner-fixable problem: fed back once."""


class _Refuse(Exception):
    def __init__(self, reason: str, status: str = "blocked") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        exp = v.as_tuple().exponent
        return int(v) if isinstance(exp, int) and exp >= 0 else float(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def sql_values(params: dict[str, Any]) -> dict[str, Any]:
    """Bind values as shown to the user and written to the audit log."""
    return {
        k: [jsonable(x) for x in v] if isinstance(v, list | tuple) else jsonable(v)
        for k, v in params.items()
    }


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float | Decimal):
        return str(v)
    if isinstance(v, list | tuple):
        return "ARRAY[" + ", ".join(_sql_literal(x) for x in v) + "]"
    if isinstance(v, datetime):
        return f"TIMESTAMPTZ '{v.isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def sql_with_values(sql: str, params: dict[str, Any]) -> str:
    """A reading copy of the SQL with the values filled in. Display only: the query itself
    always runs with bind parameters."""
    for name in sorted(params, key=len, reverse=True):
        sql = re.sub(rf"(?<![:\w]):{re.escape(name)}\b", _sql_literal(params[name]), sql)
    return sql


def fmt_value(v: Any, unit: str) -> str:
    if v is None:
        return "-"
    if unit == "ratio" and isinstance(v, int | float | Decimal):
        return f"{float(v) * 100:.1f}%"
    if isinstance(v, int) or (isinstance(v, Decimal) and v == v.to_integral_value()):
        return f"{int(v):,}"
    if isinstance(v, float | Decimal):
        return f"{float(v):,.{3 if unit == 'KWD' else 1}f}"
    return str(v)


def templated_answer(o: _Outcome, period_label: str | None) -> str:
    if not o.rows:
        return "No matching data was found for your branches."
    when = f" ({period_label})" if period_label else ""
    unit = f" {o.unit}" if o.unit and o.unit not in ("ratio", "KWD") else ""
    if o.totals:
        tot = "; ".join(
            f"{' / '.join(str(x) for x in r[:-1]) or 'All'}: {fmt_value(r[-1], o.unit)}"
            for r in o.totals
        )
        return f"{o.label or 'Result'}{when}, total: {tot}{unit}. The table shows each period."
    if len(o.columns) == 1 and len(o.rows) == 1:
        return f"{o.label or o.columns[0]}{when}: {fmt_value(o.rows[0][0], o.unit)}{unit}."
    parts = [
        f"{' / '.join(str(x) for x in r[:-1])}: {fmt_value(r[-1], o.unit)}" for r in o.rows[:5]
    ]
    more = f" (and {o.row_count - 5} more)" if o.row_count > 5 else ""
    return f"{o.label or 'Result'}{when}: " + "; ".join(parts) + more + "."


def series_totals(columns: list[str], rows: list[list[Any]]) -> list[list[Any]]:
    """Totals over the time buckets of a trend: one per series (e.g. Mobile, Web), or one
    overall. Only for additive metrics (count, sum); computed in code, never by the LLM."""
    if not columns or columns[0] != "period" or columns[-1] != "value":
        return []
    sums: dict[tuple[Any, ...], Decimal] = {}
    for r in rows:
        if r[-1] is None:
            continue
        key = tuple(r[1:-1])
        sums[key] = sums.get(key, Decimal(0)) + Decimal(str(r[-1]))
    out = [[*k, int(v) if v == v.to_integral_value() else v] for k, v in sums.items()]
    return sorted(out, key=lambda r: r[-1], reverse=True)


def _numbers(obj: Any) -> list[Any]:
    """All numeric leaves of an API response (e.g. totals the table does not show)."""
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers(v)]
    return [obj] if isinstance(obj, int | float) and not isinstance(obj, bool) else []


@cache
def _branch_names() -> dict[str, str]:
    with psycopg.connect(get_settings().agent_url, connect_timeout=5) as conn:
        rows = conn.execute("SELECT branch_code, branch_name FROM core.branch_master").fetchall()
    return {r[0]: r[1] for r in rows}


class DataAgent:
    def __init__(
        self,
        provider: LLMProvider,
        embedder: EmbeddingProvider | None = None,
        model: SemanticModel | None = None,
        obdx: ObdxClient | None = None,
        audit: Callable[[AuditRecord], None] | None = write_audit,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model or get_model()
        self.registry = build_registry(self.model)
        self.index = ExampleIndex(list(self.model.examples), embedder)
        self.obdx = obdx or ObdxClient()
        self.audit = audit
        self.settings = get_settings()
        self.today = today or (lambda: datetime.now(self.settings.tz).date())
        self.system = prompts.planner_system(self.model)

    # ------------------------------------------------------------------ helpers
    def _egress(self, text: str) -> str:
        return text if self.provider.name in LOCAL_PROVIDERS else mask_pii(text)

    async def _run_sql(
        self, exec_sql: str, params: dict[str, Any], auth: AuthContext
    ) -> tuple[list[str], list[list[Any]], int, bool]:
        res = await asyncio.to_thread(
            run_readonly, exec_sql, params, auth.branches, max_rows=self.settings.max_rows
        )
        return res.columns, [list(r) for r in res.rows], res.row_count, res.truncated

    # ---------------------------------------------------------------- executors
    async def _exec_metric(self, mq: MetricQuery, auth: AuthContext) -> tuple[_Outcome, Any]:
        try:
            cq = compile_query(self.model, mq, auth.branches, self.today())
        except UnknownValue:
            raise  # the planner loop decides: ask the user, or let the planner fix it
        except SemanticError as e:
            raise _Retry(str(e)) from e
        v = validate_sql(
            self.model, cq.tree, user_branches=auth.branches, source="compiled", params=cq.params
        )
        cols, rows, n, trunc = await self._run_with_limits(v.exec_sql, v.params, auth)
        metric = self.model.metrics[mq.metric]
        out = _Outcome(
            "metric",
            cols,
            rows,
            n,
            trunc,
            cq.definition,
            v.sql,
            mq.model_dump(by_alias=True, exclude_none=True, mode="json"),
            metric.unit,
            metric.label,
        )
        out.params = v.params
        if mq.grain and metric.measure.agg in ("count", "sum") and not trunc:
            out.totals = series_totals(cols, rows)
        return out, cq.period

    async def _exec_sql(self, args: WriteSqlArgs, auth: AuthContext) -> _Outcome:
        try:
            v = validate_sql(self.model, args.sql, user_branches=auth.branches, source="llm")
        except SQLRejected as e:
            if e.code in SECURITY_CODES or e.code in _SQL_REFUSAL:
                raise _Refuse(_SQL_REFUSAL.get(e.code, "not_allowed")) from e
            raise _Retry(str(e)) from e
        try:
            cols, rows, n, trunc = await self._run_with_limits(v.exec_sql, v.params, auth)
        except psycopg.errors.InsufficientPrivilege as e:  # DB layer caught something
            raise _Refuse("blocked_column") from e
        except psycopg.Error as e:
            diag = getattr(getattr(e, "diag", None), "message_primary", None) or str(e)
            raise _Retry(f"The database rejected the SQL: {diag}") from e
        definition = (
            "Free SQL fallback (validated, read-only, limited to your branches). "
            f"Reason: {args.reason}"
        )
        out = _Outcome("sql", cols, rows, n, trunc, definition, v.sql, label="Result")
        out.params = v.params
        return out

    async def _run_with_limits(
        self, exec_sql: str, params: dict[str, Any], auth: AuthContext
    ) -> tuple[list[str], list[list[Any]], int, bool]:
        try:
            return await self._run_sql(exec_sql, params, auth)
        except SQLRejected as e:  # cost ceiling
            raise _UserError(e.code, str(e)) from e
        except psycopg.errors.QueryCanceled as e:
            raise _UserError(
                "timeout", "The query took too long (10 s limit). Narrow the period or add filters."
            ) from e

    async def _exec_api(self, args: CallApiArgs, auth: AuthContext) -> _Outcome:
        try:
            api_args: BaseModel = API_ARGS[args.name].model_validate(args.args)
        except ValidationError as e:
            raise _Retry(f"Invalid args for {args.name}: {e.errors()[0]['msg']}") from e
        try:
            r = await self.obdx.call(args.name, api_args, auth)
        except ApiNotFound:
            return _Outcome(
                "api",
                [],
                [],
                0,
                False,
                "No digital user with that ID was found in your branches.",
                label=args.name,
            )
        return _Outcome(
            "api",
            r.columns,
            r.rows,
            len(r.rows),
            False,
            r.definition,
            label=args.name.replace("get_", "").replace("_", " ").capitalize(),
            evidence=_numbers(r.raw),
            facts={
                k: v
                for k, v in r.raw.items()
                if isinstance(v, int | float) and not isinstance(v, bool)
            },
        )

    # --------------------------------------------------------------------- run
    async def run(
        self, question: str, auth: AuthContext, ctx: ConversationContext | None = None
    ) -> AsyncIterator[Event]:
        ctx = ctx or ConversationContext()
        t0 = time.perf_counter()
        msg_id = uuid.uuid4()
        rec = AuditRecord(
            username=auth.username,
            user_role=auth.role,
            question=question,
            route="refused",
            status="error",
            llm_provider=self.provider.name,
            llm_model=self.provider.model,
            conversation_id=ctx.conversation_id,
            message_id=msg_id,
        )
        tokens = [0, 0]
        answer_text = ""
        outcome: _Outcome | None = None
        grounded: bool | None = None
        fallback: str | None = None
        arabic = bool(_ARABIC.search(question))
        unknown: tuple[str, list[str]] | None = None  # (name the user typed, valid names)

        async def _steps() -> AsyncIterator[Event]:
            nonlocal question, answer_text, outcome, grounded, fallback, unknown
            question = question.strip()
            if not question or len(question) > MAX_QUESTION_CHARS:
                rec.error = "empty or too long"
                yield {
                    "type": "error",
                    "code": "bad_request",
                    "message": f"Questions must be 1 to {MAX_QUESTION_CHARS} characters.",
                }
                return
            if ctx.pending_clarification is not None:
                merged = answer_clarification(ctx.pending_clarification, question)
                if merged is not None:
                    question = merged
                    rec.question = merged
            ctx.pending_clarification = None
            yield {"type": "status", "stage": "understanding"}
            examples = await self.index.top_k(question, k=4)
            branch_names = [f"{_branch_names().get(b, b)} ({b})" for b in auth.branches]
            user_prompt = prompts.planner_user(
                self._egress(question),
                today=self.today(),
                tz=self.settings.tz_name,
                role=auth.role,
                branch_names=branch_names,
                examples=examples,
                previous_query=ctx.last_metric_query,
                turns=[(r, self._egress(t)) for r, t in ctx.turns],
            )
            messages = [Message(role="user", content=user_prompt)]
            specs = self.registry.all_specs()  # roles are enforced in registry.parse
            period = None
            for attempt in (1, 2):
                res = await self.provider.complete(
                    self.system,
                    messages,
                    tools=specs,
                    require_tool=True,
                    temperature=0.0,
                    max_tokens=800,
                )
                tokens[0] += res.input_tokens
                tokens[1] += res.output_tokens
                fallback = res.fallback or fallback
                feedback: str | None = None
                call = res.tool_call
                if call is None:
                    feedback = "You must call exactly one of the tools."
                else:
                    route = ROUTES.get(call.name, "refused")
                    yield {
                        "type": "plan",
                        "route": route,
                        "tool": call.name,
                        "args": call.args,
                        "attempt": attempt,
                    }
                    try:
                        args = self.registry.parse(call, auth.role)
                        if isinstance(args, MetricQuery):  # drop unrequested grain / top-N
                            trimmed, removed = trim_extras(
                                args.model_dump(by_alias=True, exclude_none=True, mode="json"),
                                question,
                                follow_up=ctx.last_metric_query is not None,
                            )
                            # A follow-up with no time words keeps the previous period.
                            prev = ctx.last_metric_query
                            if (
                                prev is not None
                                and prev.get("time")
                                and not names_period(question)
                                and trimmed.get("time") != prev["time"]
                                and self.model.metrics[args.metric].kind != "current"
                            ):
                                trimmed["time"] = prev["time"]
                                removed.append("changed period")
                            if removed:
                                log.info("guard adjusted plan: %s", removed)
                                args = MetricQuery.model_validate(trimmed)
                        metric = (
                            self.model.metrics.get(args.metric)
                            if isinstance(args, MetricQuery)
                            else None
                        )
                        if (
                            metric is not None
                            and metric.kind != "current"
                            and ctx.last_metric_query is None
                            and not mentions_time(question)
                        ):
                            # Never run on an assumed period: ask (deterministic guard).
                            q_text, options = period_clarification(question)
                            outcome = _Outcome(
                                "clarify", clarify=ClarifyArgs(question=q_text, options=options)
                            )
                        elif isinstance(args, MetricQuery):
                            yield {"type": "status", "stage": "querying"}
                            outcome, period = await self._exec_metric(args, auth)
                        elif isinstance(args, WriteSqlArgs):
                            yield {"type": "status", "stage": "querying"}
                            outcome = await self._exec_sql(args, auth)
                        elif isinstance(args, CallApiArgs):
                            yield {"type": "status", "stage": "querying"}
                            outcome = await self._exec_api(args, auth)
                        elif isinstance(args, ClarifyArgs):
                            outcome = _Outcome("clarify", clarify=args)
                        elif isinstance(args, DeclineArgs):
                            outcome = _Outcome("refused", refusal=args.reason, status="ok")
                        break
                    except UnknownValue as e:
                        if e.value.casefold() not in question.casefold():
                            feedback = str(e)  # the planner mangled a name: let it fix it
                        else:  # the user typed a name we don't have: ask, never guess
                            allowed = e.allowed
                            if e.dimension == "branch":  # suggest only the user's branches
                                names = _branch_names()
                                allowed = [names.get(b, b) for b in auth.branches]
                            q_text, options = unknown_value_clarification(
                                e.dimension, e.value, allowed, arabic
                            )
                            outcome = _Outcome(
                                "clarify",
                                clarify=ClarifyArgs.model_construct(
                                    question=q_text, options=options
                                ),
                            )
                            unknown = (e.value, allowed)
                            break
                    except ToolForbidden as e:
                        raise _Refuse("role") from e
                    except (ToolError, _Retry) as e:
                        feedback = str(e)
                if attempt == 1 and feedback:
                    shown = json.dumps(call.args, ensure_ascii=False) if call else "no tool"
                    messages += [
                        Message(
                            role="assistant",
                            content=f"(called {call.name if call else 'nothing'}: {shown})",
                        ),
                        Message(
                            role="user",
                            content=f"That did not work: {feedback}\n"
                            "Fix it and call exactly one tool again.",
                        ),
                    ]
                    log.info("planner retry: %s", feedback)
            # The planner declined or failed, but the question names something close to one
            # of the user's branches ("Hawaii" for "Hawalli"): ask rather than refuse.
            # Personal-data and raw-document refusals are never overridden.
            if outcome is None or outcome.refusal in ("other", "not_data_question"):
                names = _branch_names()
                miss = near_miss(question, [names.get(b, b) for b in auth.branches])
                if miss is not None:
                    q_text, options = unknown_value_clarification(
                        "branch", miss[0], miss[1], arabic
                    )
                    outcome = _Outcome(
                        "clarify",
                        clarify=ClarifyArgs.model_construct(question=q_text, options=options),
                    )
                    unknown = (miss[0], miss[1])
            if outcome is None:
                rec.route = ROUTES.get(call.name, "refused") if call else "refused"
                rec.error = f"planner failed after retry: {feedback}"
                yield {
                    "type": "error",
                    "code": "plan_failed",
                    "message": "I couldn't turn that into a query. Try rephrasing, or "
                    "name the metric and period.",
                }
                return

            rec.route = outcome.route
            rec.metric_query = outcome.metric_query
            rec.sql_text = (
                f"{outcome.sql}\n-- values: {json.dumps(sql_values(outcome.params))}"
                if outcome.sql and outcome.params
                else outcome.sql
            )
            if outcome.clarify is not None:
                rec.status = "ok"
                answer_text = outcome.clarify.question + " " + " / ".join(outcome.clarify.options)
                yield {
                    "type": "clarify",
                    "question": outcome.clarify.question,
                    "options": outcome.clarify.options,
                }
                return
            if outcome.refusal is not None:
                raise _Refuse(outcome.refusal, status=outcome.status)

            rows_json = [[jsonable(c) for c in r] for r in outcome.rows]
            rec.row_count = outcome.row_count
            yield {
                "type": "result",
                "columns": outcome.columns,
                "rows": rows_json,
                "row_count": outcome.row_count,
                "truncated": outcome.truncated,
                "unit": outcome.unit,
                "label": outcome.label,
                "totals": [[jsonable(c) for c in r] for r in outcome.totals],
                "totals_columns": outcome.columns[1:] if outcome.totals else [],
            }
            chart = choose_chart(outcome.columns, outcome.rows)
            yield {"type": "chart", "chart_type": chart.pop("type"), **chart}
            yield {"type": "status", "stage": "answering"}
            answer_text, grounded = await self._answer(
                question, outcome, auth, branch_names, period.label() if period else None, tokens
            )
            yield {"type": "answer_delta", "text": answer_text}
            yield {"type": "definition", "text": outcome.definition}
            if outcome.sql:
                yield {
                    "type": "sql",
                    "text": outcome.sql,  # what ran, with bind parameters
                    "params": sql_values(outcome.params),
                    "text_with_values": sql_with_values(outcome.sql, outcome.params),
                }
            rec.status = "ok"

        try:
            async for ev in _steps():
                yield ev
        except _Refuse as r:
            rec.route = "refused"
            rec.status = r.status
            rec.error = f"refused: {r.reason}"
            answer_text = REFUSALS.get(r.reason, REFUSALS["not_allowed"])[1 if arabic else 0]
            yield {"type": "answer_delta", "text": answer_text}
        except _UserError as e:
            rec.error = f"{e.code}: {e}"
            yield {"type": "error", "code": e.code, "message": str(e)}
        except psycopg.OperationalError as e:
            rec.error = f"db_unavailable: {str(e).splitlines()[0]}"
            yield {
                "type": "error",
                "code": "db_unavailable",
                "message": "The reporting database is not reachable. Try again shortly.",
            }
        except LLMError as e:
            rec.error = f"llm: {e}"
            yield {
                "type": "error",
                "code": "llm_unavailable",
                "message": "The language model is not responding. Try again shortly.",
            }
        except Exception as e:  # never leak internals to the user
            log.exception("data agent failure")
            rec.error = f"internal: {type(e).__name__}: {e}"
            yield {
                "type": "error",
                "code": "internal",
                "message": "Something went wrong while answering. It has been logged.",
            }
        finally:
            rec.latency_ms = int((time.perf_counter() - t0) * 1000)
            rec.input_tokens, rec.output_tokens = tokens
            if rec.status != "error" or answer_text:
                ctx.remember(
                    question,
                    answer_text,
                    outcome.metric_query if outcome and rec.status == "ok" else None,
                )
            if outcome is not None and outcome.clarify is not None:
                if unknown is not None:
                    ctx.pending_clarification = PendingClarification(
                        question, tuple(unknown[1]), replace=unknown[0]
                    )
                else:
                    ctx.pending_clarification = PendingClarification(
                        question, tuple(outcome.clarify.options)
                    )
            else:
                ctx.pending_clarification = None
            if self.audit is not None:
                try:
                    await asyncio.to_thread(self.audit, rec)
                except Exception:  # audit failure must be visible in logs
                    log.exception("AUDIT WRITE FAILED for message %s", msg_id)
        yield {
            "type": "done",
            "message_id": str(msg_id),
            "route": rec.route,
            "status": rec.status,
            "provider": self.provider.name,
            "model": self.provider.model,
            "latency_ms": rec.latency_ms,
            "grounded": grounded,
            "retrieval": self.index.mode,
            "fallback": fallback,
            "tokens": {"input": tokens[0], "output": tokens[1]},
        }

    async def _answer(
        self,
        question: str,
        o: _Outcome,
        auth: AuthContext,
        branch_names: list[str],
        period_label: str | None,
        tokens: list[int],
    ) -> tuple[str, bool]:
        llm_rows = [[jsonable(c) for c in r] for r in o.rows[: self.settings.llm_rows]]
        definition = o.definition
        if o.facts:
            definition += f" Totals from the API: {json.dumps(o.facts)}"
        prompt = prompts.answer_user(
            self._egress(question),
            definition=definition,
            scope=f"{auth.role}, branches: {', '.join(branch_names)}",
            unit=o.unit,
            columns=o.columns,
            rows=llm_rows,
            truncated=o.truncated or len(o.rows) > len(llm_rows),
            totals=[[jsonable(c) for c in r] for r in o.totals],
        )
        try:
            parts = [
                p
                async for p in self.provider.stream(
                    prompts.ANSWER_RULES,
                    [Message(role="user", content=prompt)],
                    temperature=0.2,
                    max_tokens=300,
                )
            ]
        except LLMError:
            return templated_answer(o, period_label), False
        text = "".join(parts).strip()
        cells = [c for r in o.rows[: self.settings.llm_rows] for c in r] + o.evidence
        cells += [c for r in o.totals for c in r]
        ok, missing = is_grounded(
            text,
            cells,
            extra_text=[question, o.definition],
            extra_numbers=[o.row_count, len(llm_rows)],
        )
        if not text or not ok:
            log.info("grounding failed (%s); using templated answer", missing)
            return templated_answer(o, period_label), False
        return text, True
