"""Database defence in depth, checked as the agent_ro role against the seeded DB.

These are the layer-3 controls (grants, RLS, read-only, timeout). The compiler and
validator layers arrive in phase 2 and get their own tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import errors

from mocks.seed.config import load_settings

pytestmark = pytest.mark.db
S = load_settings()


def _connect(url: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(url, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"seeded database not reachable ({e.__class__.__name__}); run make up seed")


@pytest.fixture
def agent() -> Iterator[psycopg.Connection[Any]]:
    conn = _connect(S.agent_url)
    yield conn
    conn.close()


@pytest.fixture
def admin() -> Iterator[psycopg.Connection[Any]]:
    conn = _connect(S.admin_url)
    yield conn
    conn.close()


def as_branches(conn: psycopg.Connection[Any], branches: str) -> psycopg.Cursor[Any]:
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.user_branches', %s, true)", (branches,))
    return cur


def scalar(cur: psycopg.Cursor[Any], q: str) -> Any:
    cur.execute(q)
    row = cur.fetchone()
    return row[0] if row else None


@pytest.mark.parametrize(
    "table,branch_sql",
    [
        ("core.cust_account", "SELECT DISTINCT branch_code FROM core.cust_account"),
        ("core.customer", "SELECT DISTINCT home_branch FROM core.customer"),
        (
            "core.transaction",
            "SELECT DISTINCT a.branch_code FROM core.transaction t "
            "JOIN core.cust_account a USING (account_no)",
        ),
        (
            "obdx.digx_user_session",
            "SELECT DISTINCT c.home_branch FROM obdx.digx_user_session s "
            "JOIN obdx.digx_user u USING (user_id) "
            "JOIN core.customer c USING (customer_no)",
        ),
        ("atum.wf_instance", "SELECT DISTINCT branch_code FROM atum.wf_instance"),
        (
            "atum.wf_stage_history",
            "SELECT DISTINCT i.branch_code FROM atum.wf_stage_history h "
            "JOIN atum.wf_instance i USING (instance_id)",
        ),
    ],
)
def test_rls_limits_rows_to_entitled_branches(
    agent: psycopg.Connection[Any], table: str, branch_sql: str
) -> None:
    cur = as_branches(agent, "001")
    cur.execute(branch_sql)
    assert {r[0] for r in cur.fetchall()} == {"001"}, table


def test_rls_matches_admin_counts(
    agent: psycopg.Connection[Any], admin: psycopg.Connection[Any]
) -> None:
    north = "005,006,007,008"
    seen = scalar(as_branches(agent, north), "SELECT count(*) FROM core.cust_account")
    truth = scalar(
        admin.cursor(),
        "SELECT count(*) FROM core.cust_account WHERE branch_code IN ('005','006','007','008')",
    )
    assert seen == truth > 0


def test_no_entitlement_means_no_rows(agent: psycopg.Connection[Any]) -> None:
    cur = agent.cursor()  # app.user_branches never set
    assert scalar(cur, "SELECT count(*) FROM core.cust_account") == 0
    assert scalar(cur, "SELECT count(*) FROM atum.wf_instance") == 0


def test_branch_filter_in_where_clause_cannot_widen_access(
    agent: psycopg.Connection[Any],
) -> None:
    cur = as_branches(agent, "001")
    assert scalar(cur, "SELECT count(*) FROM core.cust_account WHERE branch_code = '002'") == 0


@pytest.mark.parametrize(
    "query",
    [
        "SELECT full_name FROM core.customer",
        "SELECT national_id FROM core.customer",
        "SELECT mobile_no FROM core.customer",
        "SELECT * FROM core.customer",
        "SELECT payload_json FROM atum.wf_instance",
        "SELECT payload_blob FROM atum.wf_instance",
        "SELECT payload_raw FROM atum.wf_instance",
        "SELECT content FROM atum.wf_document",
        "SELECT ocr_json FROM atum.wf_document",
        "SELECT * FROM atum.decode_error",
        "SELECT * FROM audit.query_log",
        "SELECT * FROM app.staff_user",
    ],
)
def test_blocked_columns_and_tables(agent: psycopg.Connection[Any], query: str) -> None:
    cur = as_branches(agent, "001")
    with pytest.raises(errors.InsufficientPrivilege):
        cur.execute(query)


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE core.cust_account SET record_stat = 'C'",
        "DELETE FROM core.transaction",
        "INSERT INTO core.branch_master VALUES ('999','x','x','x')",
        "CREATE TABLE core.evil (x int)",
    ],
)
def test_agent_cannot_write(agent: psycopg.Connection[Any], stmt: str) -> None:
    cur = as_branches(agent, "001")
    with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
        cur.execute(stmt)


def test_statement_timeout_is_10s(agent: psycopg.Connection[Any]) -> None:
    assert scalar(agent.cursor(), "SHOW statement_timeout") == "10s"


def test_refresh_live_sets_exact_active_count_and_is_repeatable(
    admin: psycopg.Connection[Any],
) -> None:
    from mocks.seed.refresh_live import main as refresh

    active_sql = (
        "SELECT count(*) FROM obdx.digx_user_session WHERE login_status = 'S' "
        "AND logout_at IS NULL AND last_activity_at >= now() - interval '15 minutes'"
    )
    total_sql = "SELECT count(*) FROM obdx.digx_user_session"
    before_total = scalar(admin.cursor(), total_sql)
    admin.rollback()
    stale_active = scalar(admin.cursor(), active_sql)  # seeded ones still inside 15 min
    admin.rollback()
    for count in (77, 77):  # running twice must not accumulate rows
        assert refresh(["--count", str(count)]) == 0
    assert scalar(admin.cursor(), total_sql) - before_total <= 77
    assert 77 <= scalar(admin.cursor(), active_sql) <= 77 + stale_active


def test_audit_log_is_append_only(admin: psycopg.Connection[Any]) -> None:
    cur = admin.cursor()
    cur.execute(
        "INSERT INTO audit.query_log (username, question, route, status) "
        "VALUES ('test', 'append-only check', 'refused', 'blocked') RETURNING id"
    )
    row = cur.fetchone()
    assert row is not None
    with pytest.raises(errors.RaiseException):
        cur.execute("UPDATE audit.query_log SET question = 'x' WHERE id = %s", (row[0],))
    admin.rollback()
