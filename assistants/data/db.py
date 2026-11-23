"""Read-only execution as agent_ro: branch entitlement set per transaction, EXPLAIN cost
check, row caps. Phase 3's executor builds on this; Phase 2 uses it in DB tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from assistants.data.semantic.compiler import check_branches
from assistants.data.semantic.validator import SQLRejected
from assistants.data.settings import get_settings


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int  # rows returned, capped at max_rows
    truncated: bool
    cost: float


def explain_cost(cur: psycopg.Cursor[Any], exec_sql: str, params: dict[str, Any]) -> float:
    cur.execute("EXPLAIN (FORMAT JSON) " + exec_sql, params)
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("EXPLAIN returned nothing")
    return float(row[0][0]["Plan"]["Total Cost"])


def run_readonly(
    exec_sql: str,
    params: dict[str, Any],
    user_branches: Sequence[str],
    *,
    max_rows: int | None = None,
    cost_ceiling: float | None = None,
    url: str | None = None,
) -> QueryResult:
    s = get_settings()
    branches = check_branches(user_branches)
    max_rows = max_rows or s.max_rows
    ceiling = cost_ceiling or s.cost_ceiling
    with psycopg.connect(url or s.agent_url, autocommit=False) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            # Equivalent to SET LOCAL: scoped to this transaction only.
            cur.execute("SELECT set_config('app.user_branches', %s, true)", (",".join(branches),))
            cur.execute("SELECT set_config('TimeZone', %s, true)", (s.tz_name,))
            cost = explain_cost(cur, exec_sql, params)
            if cost > ceiling:
                raise SQLRejected(
                    "too_expensive",
                    f"Query is too expensive to run (cost {cost:,.0f} > "
                    f"{ceiling:,.0f}). Narrow the period or add filters.",
                )
            cur.execute(exec_sql, params)
            columns = [d.name for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
        conn.rollback()
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return QueryResult(columns, [tuple(r) for r in rows], len(rows), truncated, cost)
