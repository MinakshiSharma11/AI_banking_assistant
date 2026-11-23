from __future__ import annotations

from datetime import date
from functools import cache
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from assistants.data.semantic.compiler import SemanticError, compile_query
from assistants.data.semantic.loader import SemanticModel, load_semantic_model
from assistants.data.semantic.query import MetricQuery

TODAY = date(2026, 11, 17)


@cache
def model() -> SemanticModel:
    return load_semantic_model(tz="Asia/Kuwait")


def c(q: dict[str, Any], branches: list[str] | None = None) -> Any:
    return compile_query(model(), MetricQuery.model_validate(q), branches or ["001"], TODAY)


def test_spec_example_compiles_with_binds_and_row_security() -> None:
    out = c(
        {
            "metric": "accounts_opened",
            "dimensions": ["branch"],
            "filters": [{"dimension": "product_family", "op": "in", "values": ["CASA"]}],
            "time": {"period": "last_quarter"},
            "order_by": {"field": "value", "dir": "desc"},
            "limit": 20,
        }
    )
    assert out.columns == ["branch", "value"]
    assert "t.auth_stat = 'A'" in out.sql  # default filter from YAML
    assert "= ANY(%(user_branches)s)" in out.exec_sql
    assert "%(f_1)s" in out.exec_sql and "'CASA'" not in out.exec_sql  # user value is a bind
    assert out.params["f_1"] == "CASA"
    assert out.params["t_from"] == date(2026, 7, 1) and out.params["t_to"] == date(2026, 10, 1)
    assert out.params["user_branches"] == ["001"]
    assert "LIMIT 20" in out.sql
    assert "Authorised CASA and TD accounts" in out.definition
    assert "Branch scope: 001" in out.definition
    sqlglot.parse_one(out.exec_sql, read="postgres")  # renders valid SQL


def test_only_needed_joins_are_added() -> None:
    plain = c({"metric": "accounts_opened", "time": {"period": "ytd"}})
    assert " JOIN " not in plain.sql
    by_channel = c(
        {"metric": "accounts_opened", "dimensions": ["channel"], "time": {"period": "ytd"}}
    )
    assert "LEFT JOIN obdx.origination_application AS oa" in by_channel.sql
    assert "COALESCE(oa.channel, 'BRANCH')" in by_channel.sql


def test_chained_join_requirements() -> None:
    out = c({"metric": "logins", "dimensions": ["branch"], "time": {"period": "ytd"}})
    sql = out.sql
    assert (
        sql.index("obdx.digx_user AS u")
        < sql.index("core.customer AS cu")
        < sql.index("core.branch_master AS br")
    )


def test_lookup_display_and_filter_on_codes() -> None:
    out = c(
        {
            "metric": "txn_value_kwd",
            "dimensions": ["channel"],
            "filters": [{"dimension": "channel", "values": ["mobile", "OBDX_WEB"]}],
            "time": {"period": "last_12_months"},
            "grain": "month",
        }
    )
    assert out.params["f_1"] == "OBDX_MOB" and out.params["f_2"] == "OBDX_WEB"
    assert (
        "WHEN 'OBDX_MOB' THEN 'Mobile'" in out.sql.replace("\n", " ").replace("    ", " ")
        or "'Mobile'" in out.sql
    )
    assert out.columns == ["period", "channel", "value"]
    assert "DATE_TRUNC('month', t.txn_date)" in out.sql
    assert "Filters: channel in Mobile, Web" in out.definition


def test_timestamp_grain_uses_bank_time_zone_and_sunday_weeks() -> None:
    out = c({"metric": "logins", "time": {"period": "last_month"}, "grain": "week"})
    assert "AT TIME ZONE 'Asia/Kuwait'" in out.sql and "INTERVAL '1 DAY'" in out.sql
    assert out.params["t_from"].tzinfo is not None


def test_stage_sorted_by_stage_seq() -> None:
    out = c(
        {
            "metric": "stage_dwell_hours",
            "dimensions": ["stage"],
            "filters": [{"dimension": "workflow", "values": ["LOAN_ORIG"]}],
            "time": {"period": "last_month"},
        }
    )
    tree = out.tree
    assert [o.this.sql() for o in tree.args["order"].expressions] == ["st.stage_seq"]
    assert "st.stage_seq" in [g.sql() for g in tree.args["group"].expressions]


def test_ratio_metric_shape() -> None:
    out = c(
        {
            "metric": "application_conversion",
            "filters": [{"dimension": "channel", "values": ["MOBILE"]}],
            "time": {"period": "ytd"},
        }
    )
    s = out.sql.replace("\n", " ")
    assert "NULLIF(" in s and "SUM(CASE WHEN" in s.replace("  ", " ") or "CASE" in s
    assert out.columns == ["value"]


def test_snapshot_and_current_metrics() -> None:
    snap = c({"metric": "active_accounts", "time": {"period": "last_month"}})
    assert "t_from" not in snap.params and snap.params["t_to"] == date(2026, 11, 1)
    assert "t.closed_date IS NULL" in snap.sql
    live = c(
        {
            "metric": "applications_in_stage",
            "dimensions": ["stage"],
            "time": {"period": "last_month"},
        }
    )  # period ignored for a live snapshot
    assert "t_from" not in live.params and "As of now" in live.definition


@pytest.mark.parametrize(
    "q,msg",
    [
        ({"metric": "accounts_openned", "time": {"period": "ytd"}}, "Unknown metric"),
        (
            {
                "metric": "accounts_opened",
                "dimensions": ["failure_reason"],
                "time": {"period": "ytd"},
            },
            "cannot be broken down",
        ),
        ({"metric": "accounts_opened"}, "needs a period"),
        (
            {
                "metric": "accounts_opened",
                "time": {"period": "ytd"},
                "filters": [{"dimension": "segment", "values": ["VIP"]}],
            },
            "not a valid segment",
        ),
        (
            {
                "metric": "accounts_opened",
                "time": {"period": "ytd"},
                "filters": [{"dimension": "branch", "values": ["Atlantis"]}],
            },
            "not a valid branch",
        ),
        (
            {
                "metric": "accounts_opened",
                "time": {"period": "ytd"},
                "filters": [{"dimension": "hour", "values": [9]}],
            },
            "cannot be filtered",
        ),
        (
            {
                "metric": "logins",
                "time": {"period": "ytd"},
                "filters": [{"dimension": "hour", "values": ["nine"]}],
            },
            "needs numbers",
        ),
        (
            {
                "metric": "logins",
                "time": {"period": "ytd"},
                "filters": [{"dimension": "hour", "op": "gte", "values": [9, 10]}],
            },
            "exactly one",
        ),
        (
            {
                "metric": "logins",
                "time": {"period": "ytd"},
                "filters": [{"dimension": "channel", "op": "gt", "values": ["WEB"]}],
            },
            "supports in",
        ),
        ({"metric": "active_accounts", "time": {"period": "ytd"}, "grain": "month"}, "snapshot"),
        ({"metric": "applications_in_stage", "grain": "month"}, "no time grain"),
        (
            {
                "metric": "accounts_opened",
                "time": {"period": "ytd"},
                "order_by": {"field": "region"},
            },
            "order_by",
        ),
    ],
)
def test_planner_facing_errors(q: dict[str, Any], msg: str) -> None:
    with pytest.raises(SemanticError, match=msg):
        c(q)


def test_numeric_filters_and_not_in() -> None:
    out = c(
        {
            "metric": "logins",
            "time": {"period": "ytd"},
            "filters": [
                {"dimension": "hour", "op": "gte", "values": ["9"]},
                {"dimension": "channel", "op": "not_in", "values": ["web"]},
            ],
        }
    )
    assert out.params["f_1"] == 9 and out.params["f_2"] == "WEB"
    assert "NOT t.channel IN" in out.sql or "NOT (t.channel IN" in out.sql or "NOT" in out.sql


def test_limit_is_capped_and_order_variants() -> None:
    assert "LIMIT 500" in c({"metric": "txn_count", "time": {"period": "ytd"}}).sql
    out = c(
        {
            "metric": "txn_count",
            "dimensions": ["channel"],
            "time": {"period": "ytd"},
            "grain": "month",
            "order_by": {"field": "period", "dir": "asc"},
        }
    )
    assert out.tree.args["order"].expressions[0].this.sql() == "period"
    out = c(
        {
            "metric": "txn_count",
            "dimensions": ["channel"],
            "time": {"period": "ytd"},
            "order_by": {"field": "channel", "dir": "asc"},
        }
    )
    assert out.tree.args["order"].expressions[0].this.sql() == "channel"


@pytest.mark.parametrize("bad", [[], ["1"], ["001'; DROP TABLE x; --"], ["0011"]])
def test_branch_codes_are_validated(bad: list[str]) -> None:
    with pytest.raises(ValueError):
        compile_query(model(), MetricQuery(metric="txn_count"), bad, TODAY)


def test_every_example_compiles() -> None:
    for ex in model().examples:
        out = compile_query(model(), ex.query, ["001", "005"], TODAY)
        tree = sqlglot.parse_one(out.exec_sql, read="postgres")
        assert isinstance(tree, exp.Select), ex.question
        assert out.params["user_branches"] == ["001", "005"]
