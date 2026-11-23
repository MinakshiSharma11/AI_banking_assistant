from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from assistants.data.semantic.loader import SemanticModelError, load_semantic_model
from assistants.data.settings import SEMANTIC_DIR

SPEC_METRICS = {
    "accounts_opened",
    "accounts_closed",
    "active_accounts",
    "digital_registrations",
    "active_digital_users",
    "logins",
    "failed_logins",
    "avg_session_minutes",
    "txn_count",
    "txn_value_kwd",
    "failed_txn_rate",
    "application_conversion",
    "applications_in_stage",
    "applications_completed",
    "stage_dwell_hours",
    "sla_breach_rate",
    "return_rate",
    "approval_rate",
    "loan_amount_requested_kwd",
    "loan_amount_approved_kwd",
    "avg_dbr_pct",
}


def test_full_catalogue_loads() -> None:
    m = load_semantic_model(tz="Asia/Kuwait")
    assert set(m.metrics) == SPEC_METRICS
    assert len(m.examples) >= 25
    assert sum(any("؀" <= ch <= "ۿ" for ch in e.question) for e in m.examples) >= 5


def test_blocked_columns_never_allowlisted() -> None:
    m = load_semantic_model(tz="Asia/Kuwait")
    for blocked in m.blocked_columns:
        schema, tbl, col = blocked.split(".")
        rule = m.allowlist.get(f"{schema}.{tbl}")
        assert rule is None or col not in rule.columns, blocked
    assert "audit.query_log" not in m.allowlist and "app.staff_user" not in m.allowlist
    assert "atum.decode_error" not in m.allowlist


def test_tz_placeholder_is_substituted() -> None:
    m = load_semantic_model(tz="Asia/Dubai")
    hour = m.entities["digx_user_session"].dimensions["hour"].expr.sql()
    assert "Asia/Dubai" in hour and "{tz}" not in hour


def test_catalog_and_tool_schema_have_enums() -> None:
    m = load_semantic_model(tz="Asia/Kuwait")
    cat = {c["metric"]: c for c in m.catalog()}
    assert cat["accounts_opened"]["dimensions"]["segment"]["values"] == [
        "Retail",
        "Premier",
        "Corporate",
    ]
    assert cat["applications_in_stage"]["needs_period"] is False
    assert "حسابات جديدة" in cat["accounts_opened"]["synonyms"]
    schema = m.metric_query_schema()
    assert set(schema["properties"]["metric"]["enum"]) == SPEC_METRICS
    assert "failure_reason" in schema["properties"]["dimensions"]["items"]["enum"]
    assert "from" in schema["$defs"]["TimeSpec"]["properties"]
    assert m.table_columns()["core.customer"] == [
        "customer_no",
        "segment",
        "home_branch",
        "created_date",
    ]


# ---- the loader rejects inconsistent YAML ---------------------------------------
@pytest.fixture
def sem(tmp_path: Path) -> Path:
    d = tmp_path / "semantic"
    shutil.copytree(SEMANTIC_DIR, d, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    return d


def _edit(path: Path, fn: object) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)  # type: ignore[operator]
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    "file,mutate,message",
    [
        (
            "model.yaml",
            lambda d: d["allowlist"]["core.customer"]["columns"].append("full_name"),
            "blocked columns",
        ),
        (
            "model.yaml",
            lambda d: d["entities"]["cust_account"]["dimensions"]["branch"].update(
                expr="br.secret_col"
            ),
            "not allowlisted",
        ),
        (
            "model.yaml",
            lambda d: d["entities"]["cust_account"]["dimensions"]["branch"].update(expr="zz.x"),
            "unknown table alias",
        ),
        (
            "model.yaml",
            lambda d: d["allowlist"]["core.cust_account"].pop("row_security"),
            "row_security or reference",
        ),
        (
            "model.yaml",
            lambda d: d["entities"]["transaction"]["joins"]["br"].update(requires=["nope"]),
            "requires undeclared",
        ),
        (
            "model.yaml",
            lambda d: d["entities"]["transaction"].update(table="audit.query_log"),
            "not allowlisted",
        ),
        (
            "metrics/accounts_opened.yaml",
            lambda d: d.update(dimensions=["branch", "colour"]),
            "not defined",
        ),
        ("metrics/accounts_opened.yaml", lambda d: d.update(entity="nope"), "unknown entity"),
        (
            "metrics/accounts_opened.yaml",
            lambda d: d.update(time_dimension="nope"),
            "time_dimension",
        ),
        (
            "metrics/accounts_opened.yaml",
            lambda d: d.update(metric="renamed"),
            "match the file name",
        ),
        (
            "metrics/accounts_opened.yaml",
            lambda d: d.update(default_filters=["national_id = '1'"]),
            "not allowlisted",
        ),
        (
            "metrics/accounts_opened.yaml",
            lambda d: d["measure"].update(agg="median"),
            "unknown agg",
        ),
        ("metrics/accounts_opened.yaml", lambda d: d.update(kind="weird"), "unknown kind"),
        (
            "metrics/active_accounts.yaml",
            lambda d: d.update(snapshot={"valid_from": "x"}),
            "snapshot",
        ),
    ],
)
def test_bad_yaml_is_rejected(sem: Path, file: str, mutate: object, message: str) -> None:
    _edit(sem / file, mutate)
    with pytest.raises(SemanticModelError, match=message):
        load_semantic_model(sem, tz="Asia/Kuwait")
