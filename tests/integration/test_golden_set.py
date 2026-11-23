"""The golden set is checked before any LLM touches it.

For every case with an expected MetricQuery, compiling that query and running it as the
case's user must reproduce the hand-written reference SQL. That validates the reference
SQL and the semantic compiler against each other, so an eval failure means the planner
chose wrongly, not that the yardstick is broken.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
import pytest

from aiplatform.gateway.auth import load_staff_user
from assistants.data.db import run_readonly
from assistants.data.eval.harness import load_cases, plan_matches, run_reference, score
from assistants.data.semantic.compiler import compile_query
from assistants.data.semantic.loader import get_model
from assistants.data.semantic.query import MetricQuery
from assistants.data.semantic.validator import validate_sql
from assistants.data.settings import get_settings

pytestmark = pytest.mark.db
CASES = load_cases()


@pytest.fixture(scope="module", autouse=True)
def _db_up() -> None:
    try:
        psycopg.connect(get_settings().agent_url, connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.skip("seeded database not reachable; run make up seed")


def test_shape_of_the_golden_set() -> None:
    assert len(CASES) == 51  # spec: >= 48 (30 EN + 18 AR), plus E31-E33 regressions
    assert sum(c.lang == "en" for c in CASES) == 33
    assert sum(c.lang == "ar" for c in CASES) == 18
    sec = [c for c in CASES if c.security]
    assert len(sec) >= 9
    assert {c.route for c in CASES} == {"metric", "sql", "api", "clarify", "refused"}
    tests = " ".join(c.tests for c in sec)
    for kind in ("PII", "injection", "row security", "write", "role gate"):
        assert kind in tests, kind


@pytest.mark.parametrize("case", [c for c in CASES if c.reference_sql], ids=lambda c: c.id)
def test_reference_sql_runs(case: Any) -> None:
    rows = run_reference(case.reference_sql, load_staff_user(case.user))
    if case.check != "key_set":  # E08's correct answer is the empty set
        assert rows, f"{case.id}: reference returned no rows; the case would be trivial"


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.expect_query and c.check != "plan"], ids=lambda c: c.id
)
def test_expected_query_reproduces_reference(case: Any) -> None:
    auth = load_staff_user(case.user)
    model = get_model()
    today = datetime.now(get_settings().tz).date()
    q = MetricQuery.model_validate(case.expect_query)
    cq = compile_query(model, q, auth.branches, today)
    v = validate_sql(
        model, cq.tree, user_branches=auth.branches, source="compiled", params=cq.params
    )
    rows = [list(r) for r in run_readonly(v.exec_sql, v.params, auth.branches).rows]
    ok, why = score(model, case, "metric", "ok", case.expect_query, rows, auth)
    assert ok, f"{case.id}: {why}"


@pytest.mark.parametrize("case", [c for c in CASES if c.check == "plan"], ids=lambda c: c.id)
def test_plan_cases_match_themselves_and_reject_wrong_plans(case: Any) -> None:
    auth = load_staff_user(case.user)
    model = get_model()
    assert plan_matches(model, case.expect_query, case.expect_query, auth)[0]
    wrong = {**case.expect_query, "dimensions": [], "filters": []}
    if wrong != case.expect_query:
        assert not plan_matches(model, case.expect_query, wrong, auth)[0]


def test_scoring_rules() -> None:
    model = get_model()
    auth = load_staff_user("ho_analyst")
    by = {c.id: c for c in CASES}
    assert score(model, by["E09"], "clarify", "ok", None, [], auth)[0]
    assert not score(model, by["E09"], "metric", "ok", None, [], auth)[0]
    assert score(model, by["E07"], "clarify", "ok", None, [], auth)[0]  # accepted alternative
    assert score(model, by["E10"], "refused", "blocked", None, [], auth)[0]
    assert not score(model, by["E10"], "sql", "ok", None, [["x"]], auth)[0]
    assert score(model, by["E08"], "sql", "ok", {}, [], auth)[0]  # correctly empty
    assert not score(model, by["E08"], "sql", "ok", {}, [["001", 17, 17]], auth)[0]
    assert score(model, by["E03"], "api", "ok", {"name": "get_active_sessions"}, [[5]], auth)[0]
    assert not score(model, by["E03"], "api", "ok", {"name": "get_session_summary"}, [], auth)[0]
    it = load_staff_user("it_ops")
    assert score(
        model, by["E29"], "api", "ok", {"name": "get_user_status"}, [["usr200000087", "Locked"]], it
    )[0]
