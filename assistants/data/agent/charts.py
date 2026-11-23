"""Chart type is chosen here, in code, never by the LLM (spec 1).

- one value -> KPI tile
- a time dimension -> line chart (x = period, series = the other dimension, if any)
- one categorical dimension with <= 15 rows -> bar chart
- anything else -> table only
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

MAX_BARS = 15


def _numeric(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def _temporal(v: Any) -> bool:
    if isinstance(v, date | datetime):
        return True
    return isinstance(v, str) and len(v) >= 7 and v[:4].isdigit() and v[4] == "-"


def choose_chart(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    if not rows or not columns:
        return {"type": "table"}
    first = rows[0]
    if len(columns) == 1 and len(rows) == 1 and _numeric(first[0]):
        return {"type": "kpi", "y": columns[0]}
    y = columns[-1]
    if not all(_numeric(r[-1]) or r[-1] is None for r in rows):
        return {"type": "table"}
    if len(columns) in (2, 3) and all(_temporal(r[0]) for r in rows):
        return {
            "type": "line",
            "x": columns[0],
            "y": y,
            "series": columns[1] if len(columns) == 3 else None,
        }
    if len(columns) == 2 and len(rows) <= MAX_BARS:
        return {"type": "bar", "x": columns[0], "y": y, "series": None}
    return {"type": "table"}
