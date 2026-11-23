"""`make sanity`: row counts and quirk percentages against the spec targets."""

from __future__ import annotations

import gzip
import json
import sys
from typing import Any

import psycopg

from mocks.seed.config import load_settings

COUNTS: list[tuple[str, str]] = [
    ("core.branch_master", "12"),
    ("core.account_class", "8"),
    ("core.txn_type", "14"),
    ("core.customer", "6,000"),
    ("core.cust_account", "9,000"),
    ("obdx.origination_application", "7,500"),
    ("obdx.digx_user", "4,500"),
    ("obdx.digx_user_session", "150,000"),
    ("core.transaction", "300,000"),
    ("app.staff_user", "4"),
    ("atum.wf_definition", "2"),
    ("atum.wf_stage", "18"),
    ("atum.wf_instance", "8,000"),
    ("atum.wf_stage_history", "~50,000"),
    ("atum.wf_document", "12,000"),
]

# (label, target, SQL returning one numeric value)
QUIRKS: list[tuple[str, str, str]] = [
    (
        "Top-2 branches share of accounts",
        "~30%",
        "SELECT 100.0*sum(n)/(SELECT count(*) FROM core.cust_account) FROM (SELECT count(*) n "
        "FROM core.cust_account GROUP BY branch_code ORDER BY 1 DESC LIMIT 2) t",
    ),
    (
        "Customers retail / premier / corporate",
        "85 / 10 / 5",
        "SELECT round(100.0*avg((segment='R')::int))||' / '||round(100.0*avg((segment='P')::int))"
        "||' / '||round(100.0*avg((segment='C')::int)) FROM core.customer",
    ),
    (
        "Accounts closed (record_stat C)",
        "~6%",
        "SELECT 100.0*avg((record_stat='C')::int) FROM core.cust_account",
    ),
    (
        "Accounts unauthorised (auth_stat U)",
        "~2%",
        "SELECT 100.0*avg((auth_stat='U')::int) FROM core.cust_account",
    ),
    (
        "Digital share of new accounts, first 3 months",
        "~20%",
        "SELECT 100.0*avg((o.account_no IS NOT NULL)::int) FROM core.cust_account a LEFT JOIN "
        "obdx.origination_application o ON o.account_no=a.account_no "
        "WHERE a.account_open_date < (SELECT min(account_open_date)+91 FROM core.cust_account)",
    ),
    (
        "Digital share of new accounts, last 3 months",
        "~55%",
        "SELECT 100.0*avg((o.account_no IS NOT NULL)::int) FROM core.cust_account a LEFT JOIN "
        "obdx.origination_application o ON o.account_no=a.account_no "
        "WHERE a.account_open_date >= (SELECT max(account_open_date)-91 FROM core.cust_account)",
    ),
    (
        "Applications submitted (incl. decided)",
        "~70%",
        "SELECT 100.0*avg((status IN ('SUBMITTED','APPROVED','REJECTED'))::int) "
        "FROM obdx.origination_application",
    ),
    (
        "Approved of decided applications",
        "~80% (see ADR-0002)",
        "SELECT 100.0*avg((status='APPROVED')::int) FROM obdx.origination_application "
        "WHERE status IN ('APPROVED','REJECTED')",
    ),
    (
        "Digital users locked / disabled",
        "3 / 1",
        "SELECT round(100.0*avg((status='L')::int),1)||' / '||round(100.0*avg((status='D')::int),1)"
        " FROM obdx.digx_user",
    ),
    (
        "Failed logins",
        "~6%",
        "SELECT 100.0*avg((login_status='F')::int) FROM obdx.digx_user_session",
    ),
    (
        "Sessions active right now (no logout, activity < 15 min)",
        "50-150",
        "SELECT count(*) FROM obdx.digx_user_session WHERE login_status='S' AND logout_at IS NULL "
        "AND last_activity_at >= now() - interval '15 minutes'",
    ),
    (
        "Transactions failed / reversed",
        "2 / 1",
        "SELECT round(100.0*avg((status='F')::int),1)||' / '||round(100.0*avg((status='R')::int),1)"
        " FROM core.transaction",
    ),
    (
        "Txn per Fri+Sat day vs per Sun-Thu day",
        "< 1 (weekday peaks)",
        "SELECT (count(*) FILTER (WHERE extract(isodow FROM txn_date) IN (5,6))/2.0) / "
        "(count(*) FILTER (WHERE extract(isodow FROM txn_date) NOT IN (5,6))/5.0) "
        "FROM core.transaction",
    ),
    (
        "Mobile share of txns, first 6 months",
        "rising",
        "SELECT 100.0*avg((channel='OBDX_MOB')::int) FROM core.transaction "
        "WHERE txn_date < (SELECT min(txn_date)+182 FROM core.transaction)",
    ),
    (
        "Mobile share of txns, last 6 months",
        "rising",
        "SELECT 100.0*avg((channel='OBDX_MOB')::int) FROM core.transaction "
        "WHERE txn_date >= (SELECT max(txn_date)-182 FROM core.transaction)",
    ),
    (
        "OBDX txns linked to a session of the same customer",
        "100%",
        "SELECT 100.0*avg((u.customer_no=c.customer_no)::int) FROM core.transaction t "
        "JOIN obdx.digx_user_session s ON s.session_id=t.session_id "
        "JOIN obdx.digx_user u ON u.user_id=s.user_id "
        "JOIN core.cust_account c ON c.account_no=t.account_no",
    ),
    (
        "wf_instance LOAN_ORIG / CASA_ONBOARD",
        "5000 / 3000",
        "SELECT count(*) FILTER (WHERE wf_code='LOAN_ORIG')||' / '||"
        "count(*) FILTER (WHERE wf_code='CASA_ONBOARD') FROM atum.wf_instance",
    ),
    (
        "Payload formats JSON / BLOB_GZIP / RAW_TEXT",
        "60 / 25 / 15",
        "SELECT round(100.0*avg((payload_format='JSON')::int))||' / '||"
        "round(100.0*avg((payload_format='BLOB_GZIP')::int))||' / '||"
        "round(100.0*avg((payload_format='RAW_TEXT')::int)) FROM atum.wf_instance",
    ),
    (
        "Loans on payload v1",
        "~30%",
        "SELECT 100.0*avg((wf_version=1)::int) FROM atum.wf_instance WHERE wf_code='LOAN_ORIG'",
    ),
    (
        "Instances still in progress",
        "~15%",
        "SELECT 100.0*avg((status='IN_PROGRESS')::int) FROM atum.wf_instance",
    ),
    (
        "Loans with a RETURN loop to DOC_VERIFY",
        "~8%",
        "SELECT 100.0*avg(r::int) FROM (SELECT bool_or(h.action='RETURN') r "
        "FROM atum.wf_instance i JOIN atum.wf_stage_history h USING (instance_id) WHERE i.wf_code='LOAN_ORIG' "
        "GROUP BY i.instance_id) t",
    ),
    (
        "SLA breaches of exited visits with an SLA",
        "~12%",
        "SELECT 100.0*avg(h.sla_breached::int) FROM atum.wf_stage_history h "
        "JOIN atum.wf_instance i USING (instance_id) JOIN atum.wf_stage s ON s.wf_code=i.wf_code AND "
        "s.stage_code=h.stage_code WHERE h.exited_at IS NOT NULL AND s.sla_hours IS NOT NULL",
    ),
    (
        "Stage with the highest breach rate",
        "CREDIT_ASSESS",
        "SELECT h.stage_code FROM atum.wf_stage_history h WHERE h.exited_at IS NOT NULL "
        "AND h.stage_code<>'DRAFT' GROUP BY 1 ORDER BY avg(h.sla_breached::int) DESC LIMIT 1",
    ),
    (
        "Documents with ocr_json",
        "~70%",
        "SELECT 100.0*avg((ocr_json IS NOT NULL)::int) FROM atum.wf_document",
    ),
    (
        "Completed CASA cases matching a core account (customer, branch, open date)",
        "100%",
        "SELECT 100.0*avg((EXISTS (SELECT 1 FROM core.cust_account a WHERE "
        "a.customer_no=i.customer_no AND a.branch_code=i.branch_code AND a.account_open_date="
        "(i.completed_at AT TIME ZONE 'Asia/Kuwait')::date))::int) FROM atum.wf_instance i "
        "WHERE wf_code='CASA_ONBOARD' AND status='COMPLETED'",
    ),
]


def _malformed(cur: psycopg.Cursor[Any]) -> tuple[int, int]:
    """Decode every payload the way the phase-2 decoder will; count failures."""
    cur.execute(
        "SELECT wf_code, payload_format, payload_json, payload_blob, payload_raw "
        "FROM atum.wf_instance"
    )
    bad = total = 0
    for wf, fmt, pj, pb, pr in cur:
        total += 1
        try:
            if fmt == "JSON":
                doc = pj
            elif fmt == "BLOB_GZIP":
                doc = json.loads(gzip.decompress(bytes(pb)))
            else:
                doc = json.loads(pr)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            if (wf == "CASA_ONBOARD" or "applicant" in doc) and not isinstance(
                doc.get("applicant"), dict
            ):
                raise ValueError("applicant is not an object")
        except Exception:  # noqa: BLE001 - counting, not handling
            bad += 1
    return bad, total


def _fmt(v: Any) -> str:
    if isinstance(v, float) or (hasattr(v, "is_finite") and not isinstance(v, int)):
        return f"{float(v):.1f}"
    return str(v)


def main() -> int:
    s = load_settings()
    with psycopg.connect(s.admin_url) as conn, conn.cursor() as cur:
        print("Row counts")
        for table, target in COUNTS:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
            n = row[0] if row else 0
            print(f"  {table:<34} {n:>9,}   target {target}")
        print("\nQuirks")
        for label, target, q in QUIRKS:
            cur.execute(q)
            row = cur.fetchone()
            print(f"  {label:<72} {_fmt(row[0] if row else None):>14}   target {target}")
        bad, total = _malformed(cur)
        print(
            f"  {'Malformed payloads (decoder error path)':<72} "
            f"{f'{bad} ({100.0 * bad / max(total, 1):.2f}%)':>14}   target ~20 (0.25%)"
        )

    print("\nRow security as agent_ro")
    with psycopg.connect(s.agent_url) as conn, conn.cursor() as cur:
        for user, branches in [
            ("bm_salmiya", "001"),
            ("rh_north", "005,006,007,008"),
            ("(no entitlement)", ""),
        ]:
            cur.execute("SELECT set_config('app.user_branches', %s, true)", (branches,))
            cur.execute("SELECT count(*), count(DISTINCT branch_code) FROM core.cust_account")
            r = cur.fetchone()
            cur.execute("SELECT count(*) FROM core.transaction")
            t = cur.fetchone()
            print(
                f"  {user:<18} accounts={r[0] if r else 0:>6,} branches={r[1] if r else 0:>2} "
                f"transactions={t[0] if t else 0:>8,}"
            )
            conn.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
