from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aiplatform.eval.run import pct, summarise, write_report
from assistants.data.eval.harness import compare_keys, compare_rows, load_cases


def test_rows_compare_as_multisets_with_type_normalisation() -> None:
    agent = [["Salmiya", 291], ["Hawalli", 149.0]]
    ref = [("hawalli", Decimal("149")), ("Salmiya", 291)]
    assert compare_rows(agent, ref)[0]
    assert not compare_rows(agent, [("Salmiya", 290)])[0]
    assert not compare_rows(agent + [["Jahra", 1]], ref)[0]
    assert compare_rows(agent + [["Jahra", 1]], ref, subset=True)[0]
    assert compare_rows(
        [["2026-01-01", "Mobile", 1.5]], [(date(2026, 1, 1), "Mobile", Decimal("1.500"))]
    )[0]


def test_key_sets_accept_code_or_name() -> None:
    ref = [("001", "Salmiya"), ("005", "Jahra")]
    assert compare_keys([["Salmiya", 3], ["005", 2]], ref)[0]
    assert not compare_keys([["Salmiya", 3]], ref)[0]
    assert not compare_keys([["Salmiya"], ["005"], ["Wafra"]], ref)[0]
    assert compare_keys([], [])[0] and not compare_keys([["x"]], [])[0]


def test_percentiles() -> None:
    assert pct([], 95) is None
    assert pct([1, 2, 3, 4, 100], 95) == 100 and pct([5], 50) == 5


def test_summary_and_report(tmp_path: Path) -> None:
    def r(i: str, route: str, ok: bool, lang: str = "en", sec: bool = False) -> dict[str, object]:
        return {
            "id": i,
            "lang": lang,
            "question": f"q {i}",
            "expected_route": route,
            "route": route if ok else "metric",
            "status": "ok",
            "passed": ok,
            "reason": "x",
            "security": sec,
            "latency_ms": 2000,
            "first_answer_ms": 1500,
            "input_tokens": 100,
            "output_tokens": 10,
            "grounded": True,
            "plan": {},
            "answer": "a",
            "attempts": 1,
            "fallback": None,
            "rows": [],
            "security_held": True if sec else None,
        }

    results = [
        r("E1", "metric", True),
        r("E2", "refused", True, sec=True),
        r("A1", "clarify", False, "ar"),
    ]
    s = summarise("ollama", "m", results, {"ollama": {"input_per_mtok": 0, "output_per_mtok": 0}})
    assert s["overall"] == (2, 3) and s["arabic"] == (0, 1) and s["security"] == (1, 1)
    assert s["security_held"] == (1, 1)
    assert s["paths"]["clarify"] == (0, 1) and s["cost_usd"] == 0
    out = tmp_path / "report.md"
    write_report(out, [{"summary": s, "results": results}], {"anthropic": "no key"})
    text = out.read_text(encoding="utf-8")
    assert "Overall execution accuracy" in text and "A1" in text and "anthropic" in text


def test_load_cases_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "g.yaml"
    bad.write_text(
        "- {id: X, lang: en, user: u, question: q, route: metric, check: rows}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="reference_sql"):
        load_cases(bad)
    bad.write_text(
        "- {id: X, lang: en, user: u, question: q, route: weird, check: route}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="bad check"):
        load_cases(bad)
