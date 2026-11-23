"""SQL validator security cases (spec section 6) without a database."""

from __future__ import annotations

from datetime import date
from functools import cache

import pytest
from sqlglot import exp, parse_one

from assistants.data.semantic.compiler import compile_query
from assistants.data.semantic.loader import SemanticModel, load_semantic_model
from assistants.data.semantic.query import MetricQuery
from assistants.data.semantic.validator import SQLRejected, ValidatedSQL, validate_sql


@cache
def model() -> SemanticModel:
    return load_semantic_model(tz="Asia/Kuwait")


def llm(sql: str, branches: list[str] | None = None) -> ValidatedSQL:
    return validate_sql(model(), sql, user_branches=branches or ["001"], source="llm")


@pytest.mark.parametrize(
    "sql,code",
    [
        # PII and never-queryable columns
        ("SELECT full_name FROM core.customer", "blocked_column"),
        ("SELECT c.national_id FROM core.customer c", "blocked_column"),
        ("SELECT mobile_no, customer_no FROM core.customer", "blocked_column"),
        ("SELECT payload_json FROM atum.wf_instance", "blocked_column"),
        ("SELECT i.payload_blob FROM atum.wf_instance i", "blocked_column"),
        (
            "SELECT payload_raw FROM atum.wf_instance WHERE reference_no = 'LN-000123'",
            "blocked_column",
        ),
        ("SELECT content FROM atum.wf_document", "blocked_column"),
        ("SELECT d.ocr_json FROM atum.wf_document d", "blocked_column"),
        ("SELECT x.full_name FROM (SELECT * FROM core.customer) x", "blocked_column"),
        ("SELECT c FROM core.customer c", "column_not_allowed"),  # whole-row reference
        ("SELECT to_json(c) FROM core.customer c", "function_not_allowed"),
        ("SELECT count(*) FROM core.customer WHERE full_name LIKE 'A%'", "blocked_column"),
        ("SELECT FULL_NAME FROM CORE.CUSTOMER", "blocked_column"),
        ('SELECT "full_name" FROM core.customer', "blocked_column"),
        ("SELECT (SELECT max(national_id) FROM core.customer) AS x", "blocked_column"),
        (
            "SELECT count(*) FROM core.cust_account a JOIN core.customer c USING (customer_no) "
            "GROUP BY c.mobile_no",
            "blocked_column",
        ),
        # tables outside the allowlist
        ("SELECT * FROM audit.query_log", "blocked_table"),
        ("SELECT * FROM app.staff_user", "blocked_table"),
        ("SELECT * FROM atum.decode_error", "blocked_table"),
        ("SELECT * FROM pg_catalog.pg_roles", "blocked_table"),
        ("SELECT * FROM information_schema.columns", "blocked_table"),
        ("SELECT count(*) FROM cust_account", "unqualified_table"),
        ("SELECT * FROM other.core.cust_account", "table_not_allowed"),
        ("SELECT * FROM generate_series(1, 10)", "table_function"),
        # writes and side effects
        ("DELETE FROM core.transaction", "not_select"),
        ("UPDATE core.cust_account SET record_stat = 'C'", "not_select"),
        ("INSERT INTO core.branch_master VALUES ('999', 'x', 'x', 'x')", "not_select"),
        ("DROP TABLE core.transaction", "not_select"),
        ("COPY core.customer TO '/tmp/x'", "not_select"),
        ("SELECT * INTO core.leak FROM core.cust_account", "not_select"),
        ("SELECT * FROM core.cust_account FOR UPDATE", "not_select"),
        (
            "WITH x AS (DELETE FROM core.transaction RETURNING *) SELECT count(*) FROM x",
            "not_select",
        ),
        ("SET app.user_branches = '001,002'", "not_select"),
        ("EXPLAIN ANALYZE SELECT 1", "not_select"),
        ("SELECT 1; DROP TABLE core.transaction", "multi_statement"),
        ("SELECT 1; SELECT 2", "multi_statement"),
        # dangerous functions
        ("SELECT pg_sleep(30)", "function_not_allowed"),
        ("SELECT set_config('app.user_branches', '001,002,003', false)", "function_not_allowed"),
        ("SELECT current_setting('app.user_branches')", "function_not_allowed"),
        ("SELECT dblink('host=x', 'select 1')", "function_not_allowed"),
        ("SELECT lo_import('/etc/passwd')", "function_not_allowed"),
        ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
        (
            "SELECT count(*) FROM core.cust_account WHERE sec.user_branches() IS NOT NULL",
            "function_not_allowed",
        ),
        # misc
        ("", "empty"),
        ("SELEC * FROM", "unparseable"),
        ("SELECT * FROM core.cust_account WHERE branch_code = :b", "placeholders"),
        ("SELECT 1 " + "+ 1 " * 3000, "too_long"),
    ],
)
def test_rejected(sql: str, code: str) -> None:
    with pytest.raises(SQLRejected) as e:
        llm(sql)
    assert e.value.code == code


def test_star_expands_to_allowed_columns_only() -> None:
    v = llm("SELECT * FROM core.customer")
    tree = parse_one(v.exec_sql, read="postgres")
    assert isinstance(tree, exp.Select)
    outer = [e.alias_or_name for e in tree.expressions]
    assert outer == ["customer_no", "segment", "home_branch", "created_date"]
    for pii in ("full_name", "national_id", "mobile_no"):
        assert pii not in v.exec_sql


def test_llm_sql_is_wrapped_with_row_security_on_every_branch_table() -> None:
    v = llm(
        "SELECT a.branch_code, count(*) FROM core.cust_account a "
        "JOIN core.transaction t ON t.account_no = a.account_no "
        "JOIN core.branch_master b ON b.branch_code = a.branch_code GROUP BY 1"
    )
    assert v.wrapped
    assert v.exec_sql.count("ANY(%(user_branches)s)") == 2  # account + transaction
    assert "JOIN core.branch_master AS b" in v.exec_sql  # reference table not wrapped
    assert v.params["user_branches"] == ["001"]
    assert v.tables == {"core.cust_account", "core.transaction", "core.branch_master"}


def test_ctes_and_subqueries_are_wrapped_too() -> None:
    v = llm(
        "WITH o AS (SELECT branch_code, count(*) AS n FROM core.cust_account GROUP BY 1) "
        "SELECT * FROM o WHERE n > (SELECT count(*) FROM atum.wf_instance)"
    )
    assert v.exec_sql.count("ANY(%(user_branches)s)") == 2


def test_limit_forced() -> None:
    assert "LIMIT 500" in llm("SELECT * FROM core.transaction").exec_sql
    assert "LIMIT 500" in llm("SELECT * FROM core.transaction LIMIT 100000").exec_sql
    assert "LIMIT 10" in llm("SELECT * FROM core.transaction LIMIT 10").exec_sql
    assert (
        "LIMIT 500"
        in llm(
            "SELECT branch_code FROM core.cust_account UNION SELECT home_branch FROM core.customer"
        ).exec_sql
    )


def test_percent_in_literals_is_escaped_for_psycopg() -> None:
    v = llm("SELECT count(*) FROM core.branch_master WHERE branch_name LIKE 'S%'")
    assert "'S%%'" in v.exec_sql and "'S%'" in v.sql


def test_allowed_functions_pass() -> None:
    llm(
        "SELECT date_trunc('month', txn_date) AS m, channel, round(avg(lcy_amount), 2), "
        "coalesce(sum(lcy_amount), 0), count(DISTINCT account_no), "
        "sum(CASE WHEN status = 'F' THEN 1 ELSE 0 END)::numeric / nullif(count(*), 0), "
        "extract(dow FROM txn_date), upper(channel), max(txn_ts), "
        "row_number() OVER (ORDER BY count(*) DESC) "
        "FROM core.transaction WHERE txn_date >= current_date - interval '30 days' "
        "AND txn_date BETWEEN '2026-01-01' AND '2026-12-31' GROUP BY 1, 2"
    )


def test_compiled_path_requires_row_security_and_params() -> None:
    q = MetricQuery.model_validate({"metric": "txn_count", "time": {"period": "ytd"}})
    cq = compile_query(model(), q, ["001"], date(2026, 11, 17))
    v = validate_sql(model(), cq.tree, user_branches=["001"], source="compiled", params=cq.params)
    assert not v.wrapped and "t_from" in v.params
    with pytest.raises(SQLRejected) as e:
        validate_sql(
            model(),
            "SELECT count(*) FROM core.transaction",
            user_branches=["001"],
            source="compiled",
        )
    assert e.value.code == "no_row_security"
    with pytest.raises(SQLRejected) as e:
        validate_sql(model(), cq.tree, user_branches=["001"], source="compiled")
    assert e.value.code == "missing_params"


def test_every_example_passes_the_validator() -> None:
    for ex in model().examples:
        cq = compile_query(model(), ex.query, ["001"], date(2026, 11, 17))
        validate_sql(model(), cq.tree, user_branches=["001"], source="compiled", params=cq.params)
