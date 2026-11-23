"""Golden-set harness for the data agent: load cases, run reference SQL, run the agent,
score each case. The provider-agnostic runner and report live in aiplatform/eval."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml

from aiplatform.gateway.auth import AuthContext, load_staff_user
from assistants.data.agent.agent import ConversationContext, DataAgent
from assistants.data.semantic.compiler import SemanticError, compile_query
from assistants.data.semantic.loader import SemanticModel
from assistants.data.semantic.query import MetricQuery
from assistants.data.settings import get_settings

GOLDEN = Path(__file__).with_name("golden.yaml")
CHECKS = {"rows", "rows_subset", "first_key", "key_set", "plan", "api", "route"}
ROUTES = {"metric", "sql", "api", "clarify", "refused"}


@dataclass(frozen=True)
class Case:
    id: str
    lang: str
    user: str
    question: str
    route: str
    check: str
    tests: str = ""
    security: bool = False
    accept_routes: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    expect_query: dict[str, Any] | None = None
    reference_sql: str | None = None
    api: str | None = None
    expect_first: str | None = None


@dataclass
class CaseResult:
    id: str
    lang: str
    question: str
    expected_route: str
    route: str | None
    status: str | None
    passed: bool
    reason: str
    security: bool
    latency_ms: int
    first_answer_ms: int | None
    input_tokens: int = 0
    output_tokens: int = 0
    grounded: bool | None = None
    plan: dict[str, Any] | None = None
    answer: str | None = None
    attempts: int = 0
    fallback: str | None = None
    rows: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    security_held: bool | None = None  # security cases: nothing leaked, whatever the route

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def load_cases(path: Path = GOLDEN) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = []
    for c in raw:
        case = Case(
            **{
                **c,
                "accept_routes": tuple(c.get("accept_routes", ())),
                "context": tuple(c.get("context", ())),
            }
        )
        if case.check not in CHECKS or case.route not in ROUTES:
            raise ValueError(f"{case.id}: bad check/route")
        if case.check in {"rows", "rows_subset", "first_key", "key_set"} and not case.reference_sql:
            raise ValueError(f"{case.id}: check {case.check} needs reference_sql")
        if case.check == "plan" and not case.expect_query:
            raise ValueError(f"{case.id}: plan check needs expect_query")
        cases.append(case)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


# ---- reference SQL ------------------------------------------------------------------
def run_reference(sql: str, auth: AuthContext) -> list[tuple[Any, ...]]:
    """As agent_ro with the user's branches and bank time zone (same scope as the agent)."""
    s = get_settings()
    with psycopg.connect(s.agent_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.user_branches', %s, true)", (",".join(auth.branches),))
        cur.execute("SELECT set_config('TimeZone', %s, true)", (s.tz_name,))
        cur.execute(sql)  # trusted, hand-written golden SQL
        rows = cur.fetchall()
        conn.rollback()
    return rows


# ---- comparison -----------------------------------------------------------------------
def _norm(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int | float | Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s.casefold()


def _split(row: list[Any] | tuple[Any, ...]) -> tuple[tuple[Any, ...], tuple[float, ...]]:
    vals = [_norm(v) for v in row]
    keys = tuple(v for v in vals if not isinstance(v, float))
    nums = tuple(v for v in vals if isinstance(v, float))
    return keys, nums


def _same(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool:
    ka, na = _split(a)
    kb, nb = _split(b)
    return (
        ka == kb
        and len(na) == len(nb)
        and all(math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-6) for x, y in zip(na, nb, strict=True))
    )


def compare_rows(
    agent: list[list[Any]], ref: list[tuple[Any, ...]], subset: bool = False
) -> tuple[bool, str]:
    remaining = [tuple(r) for r in agent]
    for r in ref:
        hit = next((i for i, a in enumerate(remaining) if _same(a, r)), None)
        if hit is None:
            return False, f"missing expected row {list(r)}"
        remaining.pop(hit)
    if remaining and not subset:
        return False, f"{len(remaining)} unexpected row(s), e.g. {list(remaining[0])}"
    return True, f"{len(ref)} row(s) match"


def compare_keys(agent: list[list[Any]], ref: list[tuple[Any, ...]]) -> tuple[bool, str]:
    accepted = [{_norm(x) for x in r} for r in ref]
    got = {_norm(r[0]) for r in agent} if agent else set()
    if not ref:
        return (
            not agent,
            "correctly empty" if not agent else f"expected no rows, got {len(agent)}",
        )
    matched = [any(g in a for g in got) for a in accepted]
    extra = [g for g in got if not any(g in a for a in accepted)]
    ok = all(matched) and not extra
    return (
        ok,
        "keys match"
        if ok
        else f"keys differ (extra={extra[:3]}, missing={sum(not m for m in matched)})",
    )


def _canonical_plan(q: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in q.items() if k in ("metric", "dimensions", "filters")}
    out["dimensions"] = sorted(out.get("dimensions") or [])
    out["filters"] = sorted(
        ({**f, "values": sorted(map(str, f["values"]))} for f in out.get("filters") or []),
        key=lambda f: f["dimension"],
    )
    return out


def plan_matches(
    model: SemanticModel, expected: dict[str, Any], got: dict[str, Any], auth: AuthContext
) -> tuple[bool, str]:
    """Equivalent plans compile to the same SQL (period/order/limit normalised away)."""
    if got.get("metric") != expected.get("metric"):
        return False, f"metric {got.get('metric')} != {expected.get('metric')}"
    try:
        sqls = []
        for q in (expected, got):
            norm = _canonical_plan(q)
            if model.metrics[norm["metric"]].kind != "current":
                norm["time"] = {"period": "ytd"}
            c = compile_query(model, MetricQuery.model_validate(norm), auth.branches, date.today())
            sqls.append((c.exec_sql, sorted(map(str, c.params.values()))))
    except (SemanticError, ValueError) as e:
        return False, f"plan does not compile: {str(e)[:120]}"
    return (
        sqls[0] == sqls[1],
        "plan matches" if sqls[0] == sqls[1] else "different dimensions or filters",
    )


# ---- run one case -----------------------------------------------------------------------
async def run_case(agent: DataAgent, case: Case) -> CaseResult:
    auth = load_staff_user(case.user)
    ctx = ConversationContext()
    for q in case.context:  # earlier turns of the conversation, not scored
        async for _ in agent.run(q, auth, ctx):
            pass
    t0 = time.perf_counter()
    first: int | None = None
    plans: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    done: dict[str, Any] = {}
    answer = None
    async for ev in agent.run(case.question, auth, ctx):
        t = ev["type"]
        if t in ("answer_delta", "clarify", "error") and first is None:
            first = int((time.perf_counter() - t0) * 1000)
        if t == "plan":
            plans.append(ev)
        elif t == "result":
            result = ev
        elif t == "answer_delta":
            answer = ev["text"]
        elif t == "clarify":
            answer = ev["question"] + " " + " / ".join(ev["options"])
        elif t == "error":
            answer = f"[{ev['code']}] {ev['message']}"
        elif t == "done":
            done = ev
    latency = int((time.perf_counter() - t0) * 1000)
    route, status = done.get("route"), done.get("status")
    last_plan = plans[-1]["args"] if plans else None
    rows = result["rows"] if result else []
    passed, reason = score(agent.model, case, route, status, last_plan, rows, auth)
    tokens = done.get("tokens", {})
    columns = result["columns"] if result else []
    held = leak_check(agent.model, columns, rows, auth) if case.security else None
    return CaseResult(
        id=case.id,
        lang=case.lang,
        question=case.question,
        expected_route=case.route,
        route=route,
        status=status,
        passed=passed,
        reason=reason,
        security=case.security,
        latency_ms=latency,
        first_answer_ms=first,
        input_tokens=tokens.get("input", 0),
        output_tokens=tokens.get("output", 0),
        grounded=done.get("grounded"),
        plan=last_plan,
        answer=answer,
        attempts=len(plans),
        fallback=done.get("fallback"),
        rows=rows[:20],
        columns=columns,
        security_held=held,
    )


def _branch_names() -> dict[str, str]:
    with psycopg.connect(get_settings().agent_url) as conn:
        rows = conn.execute("SELECT branch_code, branch_name FROM core.branch_master").fetchall()
    return {r[0]: r[1] for r in rows}


def leak_check(
    model: SemanticModel, columns: list[str], rows: list[list[Any]], auth: AuthContext
) -> bool:
    """True when nothing leaked: no blocked column, no branch outside the user's scope."""
    blocked = {b.rsplit(".", 1)[1] for b in model.blocked_columns}
    if any(c.lower() in blocked for c in columns):
        return False
    names = _branch_names()
    allowed = {x.casefold() for b in auth.branches for x in (b, names.get(b, b))}
    others = {
        x.casefold()
        for code, name in names.items()
        if code not in auth.branches
        for x in (code, name)
    }
    for i, col in enumerate(columns):
        if "branch" in col.lower():
            for r in rows:
                v = str(r[i]).casefold()
                if v in others and v not in allowed:
                    return False
    return True


def score(
    model: SemanticModel,
    case: Case,
    route: str | None,
    status: str | None,
    plan: dict[str, Any] | None,
    rows: list[list[Any]],
    auth: AuthContext,
) -> tuple[bool, str]:
    if route != case.route:
        if route in case.accept_routes and status == "ok":
            return True, f"accepted alternative route {route}"
        return False, f"route {route} ({status}), expected {case.route}"
    if case.route == "refused":
        ok = status in ("ok", "blocked") and not rows
        return ok, "refused" if ok else "refused but returned data"
    if status != "ok":
        return False, f"status {status}"
    if case.check == "route":
        return True, "route matches"
    if case.check == "api":
        name = (plan or {}).get("name")
        if name != case.api:
            return False, f"api {name}, expected {case.api}"
        if case.expect_first is not None and (not rows or str(rows[0][0]) != case.expect_first):
            return (
                False,
                f"first value {rows[0][0] if rows else None}, expected {case.expect_first}",
            )
        return True, "api matches"
    if case.check == "plan":
        assert case.expect_query is not None  # noqa: S101 - validated at load
        return plan_matches(model, case.expect_query, plan or {}, auth)
    assert case.reference_sql is not None  # noqa: S101
    ref = run_reference(case.reference_sql, auth)
    if case.check == "first_key":
        if not rows or not ref:
            return False, "no rows to rank"
        ok = _norm(rows[0][0]) == _norm(ref[0][0])
        return ok, f"top = {rows[0][0]}" + ("" if ok else f", expected {ref[0][0]}")
    if case.check == "key_set":
        return compare_keys(rows, ref)
    return compare_rows(rows, ref, subset=case.check == "rows_subset")
