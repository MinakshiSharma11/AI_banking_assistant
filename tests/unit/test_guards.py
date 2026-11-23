from __future__ import annotations

import pytest

from assistants.data.agent.guards import (
    PendingClarification,
    answer_clarification,
    mentions_time,
    names_period,
    near_miss,
    trim_extras,
    unknown_value_clarification,
)


@pytest.mark.parametrize(
    "q",
    [
        "accounts opened last quarter",
        "logins right now",
        "transactions in 2026",
        "Q2 closures",
        "failed logins this week",
        "since March",
        "value by month",
        "كم عدد الحسابات هذا الشهر؟",
        "المعاملات الأسبوع الماضي",
        "الطلبات قيد الإجراء الآن",
        "active accounts today",
    ],
)
def test_time_expressions_found(q: str) -> None:
    assert mentions_time(q)


@pytest.mark.parametrize(
    "q",
    [
        "How many accounts were opened?",
        "Which stage breaches SLA most often?",
        "كم عدد الحسابات التي تم فتحها؟",
        "conversion rate of mobile applications",
    ],
)
def test_no_time_expression(q: str) -> None:
    assert not mentions_time(q)


def test_clarification_reply_is_combined_with_the_question() -> None:
    pending = PendingClarification(
        "give me accounts opened by branches", ("This month", "Last month", "This year")
    )
    assert (
        answer_clarification(pending, "this month ")
        == "give me accounts opened by branches — this month"
    )
    assert answer_clarification(pending, "last 2 weeks") is not None  # short typed answer
    assert answer_clarification(pending, "") is None


def test_long_reply_is_a_new_question() -> None:
    pending = PendingClarification("accounts opened", ("This month",))
    assert answer_clarification(pending, "how many loans were approved at Salmiya branch") is None


def test_did_you_mean_swaps_the_name() -> None:
    pending = PendingClarification(
        "accounts opened by Hawaii last quarter", ("Salmiya", "Hawalli"), replace="Hawaii"
    )
    assert answer_clarification(pending, "hawalli") == "accounts opened by Hawalli last quarter"


def test_unknown_value_suggestions() -> None:
    branches = ["Salmiya", "Kuwait City Main", "Hawalli", "Sharq", "Jahra", "Doha"]
    q, opts = unknown_value_clarification("branch", "Hawaii", branches, arabic=False)
    assert q == "There's no branch called 'Hawaii'. Did you mean:" and opts[0] == "Hawalli"
    q, opts = unknown_value_clarification("branch", "Deira", branches, arabic=False)
    assert "Available: Salmiya, Kuwait City Main" in q and len(opts) == 4
    q, _ = unknown_value_clarification("branch", "Deira", branches, arabic=True)
    assert q.startswith("لا يوجد فرع")


def test_near_miss_finds_misspelt_branch_only() -> None:
    names = ["Salmiya", "Kuwait City Main", "Hawalli", "Sharq"]
    assert near_miss("accounts opened by Hawaii last quarter", names) == ("Hawaii", ["Hawalli"])
    assert near_miss("accounts opened by Hawalli last quarter", names) is None
    assert near_miss("what is the weather today", names) is None


@pytest.mark.parametrize(
    ("query", "question", "follow_up", "removed"),
    [
        ({"grain": "month"}, "give me the total value for web and mobile", True, ["grain"]),
        ({"grain": "month"}, "Now only for Salmiya", True, []),
        ({"grain": "day"}, "Transaction count by transaction group last week", False, ["grain"]),
        ({"grain": "month"}, "Mobile vs web transaction value by month", False, []),
        ({"grain": "month"}, "إجمالي قيمة المعاملات حسب القناة", True, ["grain"]),
        ({"grain": "month"}, "قيمة المعاملات حسب القناة هذا الشهر", False, ["grain"]),
        (
            {"limit": 1, "order_by": {"field": "value", "dir": "desc"}},
            "Failed transaction rate by channel this month",
            False,
            ["ranking"],
        ),
        ({"limit": 5}, "top 5 branches by accounts opened", False, []),
        ({"limit": 5}, "accounts opened in the last 12 months", False, ["ranking"]),
    ],
)
def test_trim_extras(
    query: dict[str, object], question: str, follow_up: bool, removed: list[str]
) -> None:
    out, got = trim_extras({"metric": "m", **query}, question, follow_up)
    assert got == removed and out["metric"] == "m"


def test_leading_now_is_not_a_period() -> None:
    assert not names_period("Now only for Salmiya")
    assert not names_period("ok, now by branch")
    assert not names_period("والآن لفرع السالمية فقط")
    assert names_period("now for last month")
    assert names_period("active users right now")
