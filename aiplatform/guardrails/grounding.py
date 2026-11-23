"""Grounding check: every number in an answer must come from the evidence.

Evidence = result cells the LLM saw, plus numbers in the question, the definition text
(period dates) and the row count. A number matches if it equals an evidence value after
rounding to the answer's precision, either as is, as a percentage (x100, for ratios), or
scaled to thousands/millions ("5.9 million"). Arabic-Indic digits are normalised.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
# Not preceded by a letter, digit, dot or hyphen: skips IDs such as LN-000123 or v2.
_NUM = re.compile(r"(?<![\w.\-])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.\-])-?\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[Decimal]:
    out = []
    for tok in _NUM.findall(text.translate(_DIGITS)):
        try:
            out.append(Decimal(tok.replace(",", "")))
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
    return out


def _evidence(values: Iterable[Any]) -> set[Decimal]:
    out: set[Decimal] = set()
    for v in values:
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int | float | Decimal):
            out.add(Decimal(str(v)))
        elif isinstance(v, datetime | date):
            out.update(Decimal(x) for x in (v.year, v.month, v.day))
        elif isinstance(v, str):
            out.update(numbers_in(v))
    return out


def _places(d: Decimal) -> int:
    exp = d.as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def is_grounded(
    answer: str,
    cells: Iterable[Any],
    extra_text: Iterable[str] = (),
    extra_numbers: Iterable[int | float] = (),
) -> tuple[bool, list[str]]:
    ev = _evidence(cells) | _evidence(extra_numbers)
    for t in extra_text:
        ev |= set(numbers_in(t))
    scaled = {c for v in ev for c in (v, v * 100, v / 1000, v / 1_000_000)}
    missing = []
    for n in numbers_in(answer):
        tol = Decimal(5).scaleb(-(_places(n) + 1))  # half a unit at the answer's precision
        if not any(abs(c - n) <= tol for c in scaled):
            missing.append(str(n))
    return (not missing, missing)
