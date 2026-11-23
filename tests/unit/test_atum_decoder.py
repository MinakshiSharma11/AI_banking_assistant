"""Decoder: every payload format, both LOAN_ORIG versions, CASA, and malformed payloads."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from assistants.data.semantic.atum_decoder import DecodeError, FieldMap, parse_payload, resolve
from assistants.data.settings import SEMANTIC_DIR

FM = FieldMap.load()

V2: dict[str, Any] = {
    "applicant": {"employmentType": "SALARIED", "monthlySalary": 1450.0},
    "loan": {"product": "PL_STD", "amountRequested": 12000.0, "tenorMonths": 48, "purpose": "CAR"},
    "credit": {"score": 712, "dbr": 0.38, "decision": "APPROVE"},
    "offer": {"amountApproved": 10000.0, "ratePct": 5.25},
}
V1: dict[str, Any] = {
    "employmentType": "SALARIED",
    "monthlySalary": 1450.0,
    "product": "PL_STD",
    "loanAmount": 12000.0,
    "tenorMonths": 48,
    "purpose": "CAR",
    "creditScore": 712,
    "dbr": 0.38,
    "decision": "APPROVE",
    "amountApproved": 10000.0,
    "ratePct": 5.25,
}
EXPECTED = {
    "product": "PL_STD",
    "purpose": "CAR",
    "amount_requested": Decimal("12000.0"),
    "tenor_months": 48,
    "salary_band": "1000-2499",
    "employment_type": "SALARIED",
    "credit_score_band": "700+",
    "dbr_pct": Decimal("38.00"),
    "decision": "APPROVE",
    "amount_approved": Decimal("10000.0"),
}


def test_v1_and_v2_decode_to_the_same_row() -> None:
    assert FM.decode("LOAN_ORIG", 2, V2) == EXPECTED
    assert FM.decode("LOAN_ORIG", 1, V1) == EXPECTED


def test_raw_salary_never_leaves_the_decoder() -> None:
    row = FM.decode("LOAN_ORIG", 2, V2)
    assert Decimal("1450") not in row.values() and 1450.0 not in row.values()


@pytest.mark.parametrize(
    "salary,band", [(350, "<1000"), (999.999, "<1000"), (1000, "1000-2499"), (2500, "2500+")]
)
def test_salary_bands(salary: float, band: str) -> None:
    doc = json.loads(json.dumps(V2))
    doc["applicant"]["monthlySalary"] = salary
    assert FM.decode("LOAN_ORIG", 2, doc)["salary_band"] == band


def test_casa_payload_and_draft_loan_with_missing_optional_fields() -> None:
    casa = {
        "applicant": {"employmentType": "STUDENT", "monthlySalary": 300.5},
        "account": {"product": "SAVREG", "currency": "KWD", "accountNo": None},
        "screening": {"ekyc": "PASS", "aml": None},
    }
    row = FM.decode("CASA_ONBOARD", 1, casa)
    assert row["product"] == "SAVREG" and row["salary_band"] == "<1000"
    assert row["amount_requested"] is None and row["decision"] is None
    draft = {"applicant": V2["applicant"], "loan": V2["loan"]}
    row = FM.decode("LOAN_ORIG", 2, draft)
    assert row["credit_score_band"] is None and row["amount_approved"] is None


@pytest.mark.parametrize(
    "fmt,pj,pb,pr",
    [
        ("JSON", V2, None, None),
        ("JSON", json.dumps(V2), None, None),
        ("BLOB_GZIP", None, gzip.compress(json.dumps(V2).encode()), None),
        ("RAW_TEXT", None, None, json.dumps(V2)),
    ],
)
def test_all_three_formats(fmt: str, pj: Any, pb: bytes | None, pr: str | None) -> None:
    assert FM.decode("LOAN_ORIG", 2, parse_payload(fmt, pj, pb, pr)) == EXPECTED


@pytest.mark.parametrize(
    "wf,ver,fmt,pj,pb,pr,msg",
    [
        ("LOAN_ORIG", 2, "BLOB_GZIP", None, b"\x00BAD-not-gzip", None, "unreadable"),
        ("LOAN_ORIG", 2, "BLOB_GZIP", None, gzip.compress(b'{"loan": {"prod'), None, "unreadable"),
        ("LOAN_ORIG", 2, "BLOB_GZIP", None, None, None, "empty"),
        ("LOAN_ORIG", 2, "RAW_TEXT", None, None, '{"applicant": {"employ', "unreadable"),
        ("LOAN_ORIG", 2, "RAW_TEXT", None, None, None, "empty"),
        ("LOAN_ORIG", 2, "XML", None, None, "<x/>", "unknown payload format"),
        (
            "LOAN_ORIG",
            2,
            "JSON",
            {"applicant": "<<unavailable>>", "loan": []},
            None,
            None,
            "expected an object",
        ),
        (
            "CASA_ONBOARD",
            1,
            "JSON",
            {"applicant": {}, "account": 0},
            None,
            None,
            "expected an object",
        ),
        ("LOAN_ORIG", 1, "JSON", {"applicant": "<<unavailable>>"}, None, None, "required"),
        ("LOAN_ORIG", 3, "JSON", V2, None, None, "no field map"),
        ("LOAN_ORIG", 2, "JSON", [1, 2], None, None, "not a JSON object"),
        (
            "LOAN_ORIG",
            2,
            "JSON",
            {**V2, "loan": {**V2["loan"], "amountRequested": "lots"}},
            None,
            None,
            "not a valid decimal",
        ),
        (
            "LOAN_ORIG",
            2,
            "JSON",
            {**V2, "loan": {**V2["loan"], "product": "X" * 11}},
            None,
            None,
            "longer than 10",
        ),
        (
            "LOAN_ORIG",
            2,
            "JSON",
            {**V2, "loan": {**V2["loan"], "tenorMonths": {"n": 1}}},
            None,
            None,
            "expected a int",
        ),
    ],
)
def test_malformed_payloads_raise(
    wf: str, ver: int, fmt: str, pj: Any, pb: bytes | None, pr: str | None, msg: str
) -> None:
    with pytest.raises(DecodeError, match=msg):
        FM.decode(wf, ver, parse_payload(fmt, pj, pb, pr))


def test_resolve_paths() -> None:
    assert resolve({"a": {"b": 1}}, "$.a.b") == 1
    assert resolve({"a": None}, "$.a.b") is None
    assert resolve({}, "$.a.b") is None
    with pytest.raises(DecodeError):
        resolve({"a": 5}, "$.a.b")


def test_new_payload_version_is_a_yaml_change(tmp_path: Path) -> None:
    raw = yaml.safe_load((SEMANTIC_DIR / "atum_fields.yaml").read_text(encoding="utf-8"))
    raw["versions"]["LOAN_ORIG"].append(3)
    raw["fields"]["amount_requested"]["paths"]["LOAN_ORIG"][3] = "$.request.amount"
    raw["fields"]["product"]["paths"]["LOAN_ORIG"][3] = "$.request.product"
    raw["fields"]["salary_band"]["paths"]["LOAN_ORIG"][3] = "$.income"
    p = tmp_path / "fields.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    row = FieldMap.load(p).decode(
        "LOAN_ORIG", 3, {"request": {"amount": 5000, "product": "PL_STD"}, "income": 3000}
    )
    assert row["amount_requested"] == Decimal("5000") and row["salary_band"] == "2500+"


def test_field_map_rejects_unknown_columns(tmp_path: Path) -> None:
    raw = yaml.safe_load((SEMANTIC_DIR / "atum_fields.yaml").read_text(encoding="utf-8"))
    raw["fields"]["monthly_salary"] = {"type": "decimal", "paths": {"LOAN_ORIG": {2: "$.x"}}}
    p = tmp_path / "fields.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="not a wf_instance_flat column"):
        FieldMap.load(p)
