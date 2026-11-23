from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from aiplatform.guardrails.grounding import is_grounded, numbers_in
from aiplatform.guardrails.pii import mask_pii
from aiplatform.llm.base import ToolCall
from aiplatform.tools.registry import ToolError, ToolForbidden
from assistants.data.agent.charts import choose_chart
from assistants.data.agent.tools import build_registry
from assistants.data.semantic.loader import load_semantic_model


@pytest.mark.parametrize(
    "answer,ok",
    [
        ("Salmiya opened 291 accounts and Kuwait City Main 250.", True),
        ("Salmiya opened 1,291 accounts.", False),
        ("The conversion rate was 58.1%.", True),  # ratio 0.5810 as a percentage
        ("The conversion rate was 58%.", True),
        ("The conversion rate was 61%.", False),
        ("Total value was 5.9 million KWD.", True),  # 5,918,473.037 scaled
        ("Total value was 5,918,473.037 KWD.", True),
        ("Together they opened 541 accounts.", False),  # a computed sum is not grounded
        ("In Q2 2026 (1 Apr – 30 Jun) there were 3 branches.", True),  # from question/definition
        ("فتح فرع السالمية ٢٩١ حساباً.", True),  # Arabic-Indic digits
        ("No matching data was found.", True),
    ],
)
def test_grounding(answer: str, ok: bool) -> None:
    rows = [
        ["Salmiya", 291],
        ["Kuwait City Main", 250],
        ["Wafra", Decimal("0.5810")],
        ["Total", Decimal("5918473.037")],
    ]
    cells = [c for r in rows for c in r]
    got, _missing = is_grounded(
        answer, cells, extra_text=["Q2 2026: 1 Apr 2026 – 30 Jun 2026"], extra_numbers=[3]
    )
    assert got is ok


def test_number_extraction() -> None:
    assert numbers_in("1,234.5 and -7 and ٣٤٫٥") == [
        Decimal("1234.5"),
        Decimal("-7"),
        Decimal("34.5"),
    ]
    assert numbers_in("LN-000123 v2") == []  # identifiers are not numbers


def test_dates_count_as_evidence() -> None:
    assert is_grounded("Peak was on 2026-11-03.", [date(2026, 11, 3), 10])[0]
    assert is_grounded("In November 2026: 10 logins.", [date(2026, 11, 1), 10])[0]


@pytest.mark.parametrize(
    "text,masked",
    [
        ("civil id 287654321012 please", "civil id [CIVIL_ID] please"),
        ("call +965 51234567", "call [MOBILE]"),
        ("mobile 61234567", "mobile [MOBILE]"),
        ("IBAN KW81CBKU0000000000001234560101", "IBAN [IBAN]"),
        ("card 4111 1111 1111 1111", "card [CARD]"),
        ("mail a.b@bank.com", "mail [EMAIL]"),
        ("accounts opened in 2026 by branch 001", "accounts opened in 2026 by branch 001"),
    ],
)
def test_pii_masking(text: str, masked: str) -> None:
    assert mask_pii(text) == masked


@pytest.mark.parametrize(
    "columns,rows,kind",
    [
        (["value"], [[42]], "kpi"),
        (["period", "value"], [[date(2026, 1, 1), 1], [date(2026, 2, 1), 2]], "line"),
        (["period", "channel", "value"], [["2026-01-01", "Mobile", 1]], "line"),
        (["branch", "value"], [["A", 1], ["B", 2]], "bar"),
        (["branch", "value"], [[str(i), i] for i in range(16)], "table"),
        (["branch", "segment", "value"], [["A", "R", 1]], "table"),
        (["user_id", "status_label"], [["u1", "Locked"]], "table"),
        (["value"], [], "table"),
    ],
)
def test_chart_rules(columns: list[str], rows: list[list[object]], kind: str) -> None:
    assert choose_chart(columns, rows)["type"] == kind


def test_registry_role_gate_and_validation() -> None:
    reg = build_registry(load_semantic_model(tz="Asia/Kuwait"))
    assert {s.name for s in reg.specs("it_support")} == {"call_api", "ask_clarification", "decline"}
    assert len(reg.specs("head_office")) == 5
    with pytest.raises(ToolForbidden):
        reg.parse(ToolCall(name="write_sql", args={"sql": "SELECT 1", "reason": "x"}), "it_support")
    with pytest.raises(ToolError, match="not available"):
        reg.parse(ToolCall(name="drop_db"), "head_office")
    with pytest.raises(ToolError, match="Invalid arguments"):
        reg.parse(
            ToolCall(name="ask_clarification", args={"question": "?", "options": ["a"]}),
            "head_office",
        )
    schema = reg.tools["build_metric_query"].spec().parameters
    assert "$defs" not in schema and "accounts_opened" in schema["properties"]["metric"]["enum"]
