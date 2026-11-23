"""Semantic layer against the seeded DB: compiled SQL runs as agent_ro and matches
hand-written reference SQL; LLM SQL is branch-filtered by the wrapper even without RLS;
the allowlist matches the real grants; the decoder reconciles all payload formats."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from assistants.data.db import run_readonly
from assistants.data.semantic.atum_decoder import decode_status, run_decoder
from assistants.data.semantic.compiler import compile_query
from assistants.data.semantic.loader import SemanticModel, get_model
from assistants.data.semantic.query import MetricQuery
from assistants.data.semantic.validator import SQLRejected, validate_sql
from assistants.data.settings import get_settings
from mocks.seed.reference import ALL_BRANCHES

pytestmark = pytest.mark.db
S = get_settings()


@pytest.fixture(scope="module")
def admin() -> Iterator[psycopg.Connection[Any]]:
    try:
        conn = psycopg.connect(S.decoder_url, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("seeded database not reachable; run make up seed")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return get_model()


def today() -> date:
    return datetime.now(S.tz).date()


def run_metric(
    model: SemanticModel, q: dict[str, Any], branches: list[str]
) -> list[tuple[Any, ...]]:
    cq = compile_query(model, MetricQuery.model_validate(q), branches, today())
    v = validate_sql(model, cq.tree, user_branches=branches, source="compiled", params=cq.params)
    return run_readonly(v.exec_sql, v.params, branches).rows


def ref(
    admin: psycopg.Connection[Any], sql: str, params: dict[str, Any] | None = None
) -> list[tuple[Any, ...]]:
    with admin.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Kuwait'")
        cur.execute(sql, params or {})
        rows = cur.fetchall()
    admin.rollback()
    return rows


def test_allowlist_matches_agent_ro_grants(
    admin: psycopg.Connection[Any], model: SemanticModel
) -> None:
    granted = ref(
        admin,
        """
        SELECT table_schema || '.' || table_name, column_name
        FROM information_schema.column_privileges
        WHERE grantee = 'agent_ro' AND privilege_type = 'SELECT'
          AND table_schema IN ('core', 'obdx', 'atum')""",
    )
    yaml_cols = {(t, c) for t, r in model.allowlist.items() for c in r.columns}
    assert set(granted) == yaml_cols


def test_every_example_runs_for_every_role_scope(model: SemanticModel) -> None:
    for branches in (ALL_BRANCHES, ["005", "006", "007", "008"], ["001"]):
        for ex in model.examples:
            run_metric(model, ex.query.model_dump(by_alias=True, exclude_none=True), branches)


def test_accounts_opened_by_branch_matches_reference(
    admin: psycopg.Connection[Any], model: SemanticModel
) -> None:
    got = run_metric(
        model,
        {"metric": "accounts_opened", "dimensions": ["branch"], "time": {"period": "last_quarter"}},
        ALL_BRANCHES,
    )
    q = compile_query(
        model,
        MetricQuery.model_validate(
            {"metric": "accounts_opened", "time": {"period": "last_quarter"}}
        ),
        ["001"],
        today(),
    )
    expected = ref(
        admin,
        """
        SELECT b.branch_name, count(*) FROM core.cust_account a
        JOIN core.branch_master b USING (branch_code)
        WHERE a.auth_stat = 'A' AND a.account_open_date >= %(f)s AND a.account_open_date < %(t)s
        GROUP BY 1""",
        {"f": q.params["t_from"], "t": q.params["t_to"]},
    )
    assert dict(got) == dict(expected) and sum(dict(got).values()) > 0


def test_salmiya_manager_sees_only_salmiya(model: SemanticModel) -> None:
    rows = run_metric(
        model,
        {"metric": "accounts_opened", "dimensions": ["branch"], "time": {"period": "ytd"}},
        ["001"],
    )
    assert [r[0] for r in rows] == ["Salmiya"]


def test_channel_split_is_cross_schema(
    admin: psycopg.Connection[Any], model: SemanticModel
) -> None:
    got = dict(
        run_metric(
            model,
            {"metric": "accounts_opened", "dimensions": ["channel"], "time": {"period": "ytd"}},
            ALL_BRANCHES,
        )
    )
    assert set(got) == {"MOBILE", "WEB", "BRANCH"}
    digital = ref(
        admin,
        """
        SELECT count(*) FROM core.cust_account a JOIN obdx.origination_application o
          ON o.account_no = a.account_no AND o.status = 'APPROVED'
        WHERE a.auth_stat = 'A' AND a.account_open_date >= date_trunc('year', current_date)""",
    )
    assert got["MOBILE"] + got["WEB"] == digital[0][0]


def test_ratio_and_stage_order(model: SemanticModel) -> None:
    ((rate,),) = run_metric(
        model,
        {
            "metric": "application_conversion",
            "filters": [{"dimension": "channel", "values": ["MOBILE"]}],
            "time": {"period": "ytd"},
        },
        ALL_BRANCHES,
    )
    assert Decimal("0") < rate < Decimal("1")
    stages = [
        r[0]
        for r in run_metric(
            model,
            {
                "metric": "stage_dwell_hours",
                "dimensions": ["stage"],
                "filters": [{"dimension": "workflow", "values": ["LOAN_ORIG"]}],
                "time": {"period": "last_12_months"},
            },
            ALL_BRANCHES,
        )
    ]
    assert stages[:4] == ["DRAFT", "KYC_CHECK", "DOC_VERIFY", "CREDIT_ASSESS"]
    top = run_metric(
        model,
        {
            "metric": "sla_breach_rate",
            "dimensions": ["stage"],
            "time": {"period": "last_12_months"},
            "order_by": {"field": "value", "dir": "desc"},
            "limit": 1,
        },
        ALL_BRANCHES,
    )
    assert top[0][0] == "CREDIT_ASSESS"
    ((rr,),) = run_metric(
        model,
        {
            "metric": "return_rate",
            "filters": [{"dimension": "stage", "values": ["DOC_VERIFY"]}],
            "time": {"period": "last_12_months"},
        },
        ALL_BRANCHES,
    )
    assert Decimal("0.02") < rr < Decimal("0.2")


def test_loan_amount_reconciles_across_formats_and_versions(
    admin: psycopg.Connection[Any], model: SemanticModel
) -> None:
    """Decode payloads independently of the decoder and compare with the metric."""
    total = Decimal("0")
    for fmt, pj, pb, pr, ver in ref(
        admin,
        """
            SELECT payload_format, payload_json, payload_blob, payload_raw, wf_version
            FROM atum.wf_instance WHERE wf_code = 'LOAN_ORIG'
              AND created_at >= date_trunc('year', now() AT TIME ZONE 'Asia/Kuwait')
                                AT TIME ZONE 'Asia/Kuwait'""",
    ):
        try:
            doc = (
                pj
                if fmt == "JSON"
                else json.loads(gzip.decompress(bytes(pb)) if fmt == "BLOB_GZIP" else pr)
            )
            amt = doc["loanAmount"] if ver == 1 else doc["loan"]["amountRequested"]
        except Exception:  # noqa: BLE001, S112 - malformed payloads are excluded by design
            continue
        total += Decimal(str(amt))
    ((got,),) = run_metric(
        model, {"metric": "loan_amount_requested_kwd", "time": {"period": "ytd"}}, ALL_BRANCHES
    )
    assert got == total and total > 0


def test_llm_sql_wrapper_filters_branches_even_without_rls(
    admin: psycopg.Connection[Any], model: SemanticModel
) -> None:
    """The owner role bypasses RLS, so this isolates the validator's wrapper."""
    v = validate_sql(
        model,
        "SELECT a.branch_code, count(*) FROM core.cust_account a "
        "JOIN core.transaction t ON t.account_no = a.account_no GROUP BY 1",
        user_branches=["001"],
        source="llm",
    )
    rows = ref(admin, v.exec_sql, v.params)
    assert [r[0] for r in rows] == ["001"]
    as_agent = run_readonly(v.exec_sql, v.params, ["001"]).rows
    assert rows == as_agent


def test_free_sql_fallback_closures_vs_openings(model: SemanticModel) -> None:
    sql = """
      WITH o AS (SELECT branch_code, count(*) AS n FROM core.cust_account
                 WHERE account_open_date >= '2026-04-01' AND account_open_date < '2026-07-01'
                 GROUP BY 1),
           c AS (SELECT branch_code, count(*) AS n FROM core.cust_account
                 WHERE closed_date >= '2026-04-01' AND closed_date < '2026-07-01' GROUP BY 1)
      SELECT b.branch_name, COALESCE(c.n, 0) AS closures, COALESCE(o.n, 0) AS openings
      FROM core.branch_master b LEFT JOIN o ON o.branch_code = b.branch_code
      LEFT JOIN c ON c.branch_code = b.branch_code"""
    v = validate_sql(model, sql, user_branches=ALL_BRANCHES, source="llm")
    res = run_readonly(v.exec_sql, v.params, ALL_BRANCHES)
    assert res.columns == ["branch_name", "closures", "openings"] and res.row_count == 12


def test_explain_cost_ceiling_rejects_runaway_queries(model: SemanticModel) -> None:
    v = validate_sql(
        model,
        "SELECT count(*) FROM core.transaction a, core.transaction b",
        user_branches=ALL_BRANCHES,
        source="llm",
    )
    with pytest.raises(SQLRejected) as e:
        run_readonly(v.exec_sql, v.params, ALL_BRANCHES)
    assert e.value.code == "too_expensive"


def test_display_rows_capped_and_truncation_flagged(model: SemanticModel) -> None:
    v = validate_sql(
        model, "SELECT txn_ref FROM core.transaction", user_branches=ALL_BRANCHES, source="llm"
    )
    res = run_readonly(v.exec_sql, v.params, ALL_BRANCHES, max_rows=100)
    assert res.row_count == 100 and res.truncated


def test_decoder_state(admin: psycopg.Connection[Any]) -> None:
    ((flat,),) = ref(admin, "SELECT count(*) FROM atum.wf_instance_flat")
    ((errs,),) = ref(admin, "SELECT count(DISTINCT instance_id) FROM atum.decode_error")
    ((total,),) = ref(admin, "SELECT count(*) FROM atum.wf_instance")
    assert errs == round(total * 0.0025) and flat == total - errs
    assert decode_status(admin) == {"backlog": 0, "errors": errs}
    admin.rollback()


def test_decoder_is_incremental(admin: psycopg.Connection[Any]) -> None:
    assert run_decoder(admin).candidates == 0  # nothing changed since the last run
    with admin.cursor() as cur:
        cur.execute("""UPDATE atum.wf_instance SET updated_at = now() WHERE instance_id = (
                         SELECT instance_id FROM atum.wf_instance_flat ORDER BY 1 LIMIT 1)""")
    admin.commit()
    stats = run_decoder(admin)
    assert (stats.candidates, stats.decoded, stats.errors) == (1, 1, 0)
