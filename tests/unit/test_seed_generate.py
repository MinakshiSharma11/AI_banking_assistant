"""Generator invariants on a small, fast dataset (no database needed)."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from datetime import datetime
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

from mocks.seed.generate import COLUMNS, Generator, Row, Volumes

TZ = ZoneInfo("Asia/Kuwait")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=TZ)


@cache
def data(seed: int = 42) -> dict[str, list[Row]]:
    return Generator(seed, NOW, TZ, Volumes().scaled(0.03)).run()


def col(table: str, name: str) -> int:
    return COLUMNS[table].index(name)


def test_deterministic_for_same_seed_and_anchor() -> None:
    a = Generator(7, NOW, TZ, Volumes().scaled(0.02)).run()
    b = Generator(7, NOW, TZ, Volumes().scaled(0.02)).run()
    assert a == b


def test_row_widths_match_columns() -> None:
    for table, rows in data().items():
        assert rows, table
        assert all(len(r) == len(COLUMNS[table]) for r in rows), table


def test_primary_keys_unique() -> None:
    d = data()
    for table in COLUMNS:
        if table == "atum.wf_stage_history":
            continue
        keys = [r[0] if table != "atum.wf_stage" else (r[0], r[1]) for r in d[table]]
        assert len(keys) == len(set(keys)), table


def test_foreign_keys_resolve() -> None:
    d = data()
    customers = {r[0] for r in d["core.customer"]}
    accounts = {r[0] for r in d["core.cust_account"]}
    users = {r[0] for r in d["obdx.digx_user"]}
    sessions = {r[0] for r in d["obdx.digx_user_session"]}
    instances = {r[0] for r in d["atum.wf_instance"]}
    assert {r[1] for r in d["core.cust_account"]} <= customers
    assert {r[1] for r in d["core.transaction"]} <= accounts
    assert {r[9] for r in d["core.transaction"]} - {None} <= sessions
    assert {r[1] for r in d["obdx.digx_user_session"]} <= users
    assert {r[2] for r in d["obdx.origination_application"]} - {None} <= accounts
    assert {r[0] for r in d["atum.wf_stage_history"]} <= instances
    assert {r[1] for r in d["atum.wf_document"]} <= instances


def test_exactly_one_payload_column_matches_format() -> None:
    i = col("atum.wf_instance", "payload_format")
    for r in data()["atum.wf_instance"]:
        fmt, pj, pb, pr = r[i], r[i + 1], r[i + 2], r[i + 3]
        assert sum(x is not None for x in (pj, pb, pr)) == 1
        assert {"JSON": pj, "BLOB_GZIP": pb, "RAW_TEXT": pr}[fmt] is not None


def test_both_loan_payload_versions_and_some_malformed() -> None:
    rows = data()["atum.wf_instance"]
    versions = Counter(r[2] for r in rows if r[1] == "LOAN_ORIG")
    assert versions[1] > 0 and versions[2] > 0
    bad = 0
    for r in rows:
        fmt, pj, pb, pr = r[11], r[12], r[13], r[14]
        try:
            doc: Any = (
                pj
                if fmt == "JSON"
                else json.loads(gzip.decompress(pb) if fmt == "BLOB_GZIP" else pr)
            )
            if not isinstance(doc.get("applicant", {}), dict):
                raise ValueError
        except Exception:  # noqa: BLE001
            bad += 1
    assert bad >= 1


def test_v1_and_v2_loan_shapes() -> None:
    for r in data()["atum.wf_instance"]:
        doc = r[12]
        if r[1] != "LOAN_ORIG" or r[11] != "JSON" or doc.get("applicant") == "<<unavailable>>":
            continue  # only well-formed JSON loan payloads
        if r[2] == 1:
            assert "loanAmount" in doc and "loan" not in doc
        else:
            assert "amountRequested" in doc["loan"] and "loanAmount" not in doc


def test_nothing_in_the_future() -> None:
    d = data()
    assert max(r[4] for r in d["obdx.digx_user_session"]) <= NOW
    assert max(r[6] for r in d["core.transaction"]) <= NOW
    for r in d["atum.wf_stage_history"]:
        assert r[2] <= NOW
        if r[3] is not None:
            assert r[2] <= r[3] <= NOW


def test_history_is_consistent_with_instance_status() -> None:
    d = data()
    last: dict[str, Row] = {}
    for h in d["atum.wf_stage_history"]:
        last[h[0]] = h
    for inst in d["atum.wf_instance"]:
        h = last[inst[0]]
        assert h[1] == inst[6]  # current_stage = last visited stage
        assert h[3] is None  # last visit is open (in progress) or an END stage
        if inst[7] == "IN_PROGRESS":
            assert inst[10] is None
        else:
            assert inst[6] in {"CLOSED", "REJECTED", "CANCELLED"} and inst[10] is not None


def test_completed_casa_cases_reconcile_with_core_accounts() -> None:
    d = data()
    acct = {(r[1], r[2], r[5]) for r in d["core.cust_account"]}
    for r in d["atum.wf_instance"]:
        if r[1] == "CASA_ONBOARD" and r[7] == "COMPLETED":
            assert (r[4], r[5], r[10].astimezone(TZ).date()) in acct


def test_active_sessions_within_planned_range() -> None:
    lo, hi = Volumes().scaled(0.03).active_sessions
    active = [
        s
        for s in data()["obdx.digx_user_session"]
        if s[7] == "S" and s[6] is None and (NOW - s[5]).total_seconds() <= 15 * 60
    ]
    assert lo <= len(active) <= hi


def test_obdx_transactions_carry_a_session() -> None:
    for t in data()["core.transaction"]:
        assert (t[9] is not None) == t[7].startswith("OBDX_")
