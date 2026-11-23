"""Resolve MetricQuery periods to concrete date ranges in the bank's time zone.

Ranges are half-open: [start, end). Quarters follow the calendar year. Weeks start on
Sunday (the Kuwait work week runs Sunday-Thursday). "this_*" periods and ytd run up to
and including today. last_12_months covers the current month and the 11 before it, so a
monthly grain gives exactly 12 buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from assistants.data.semantic.query import TimeSpec

WEEK_START = 6  # date.weekday(): Monday=0 ... Sunday=6


@dataclass(frozen=True)
class ResolvedPeriod:
    name: str
    start: date  # inclusive
    end: date  # exclusive

    @property
    def last_day(self) -> date:
        return self.end - timedelta(days=1)

    def label(self) -> str:
        nice = self.name.replace("_", " ")
        if self.start == self.last_day:
            return f"{nice} ({_fmt(self.start)})"
        return f"{nice} ({_fmt(self.start)} – {_fmt(self.last_day)})"

    def bounds(self, kind: str, tz: ZoneInfo) -> tuple[date | datetime, date | datetime]:
        """Bind values for a column of type 'date' or 'timestamp' (bank-local midnight)."""
        if kind == "date":
            return self.start, self.end
        return (
            datetime.combine(self.start, time(), tzinfo=tz),
            datetime.combine(self.end, time(), tzinfo=tz),
        )


def _fmt(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _week_start(d: date) -> date:
    return d - timedelta(days=(d.weekday() - WEEK_START) % 7)


def resolve_period(spec: TimeSpec, today: date) -> ResolvedPeriod:
    p = spec.period
    tomorrow = today + timedelta(days=1)
    match p:
        case "today":
            return ResolvedPeriod(p, today, tomorrow)
        case "yesterday":
            return ResolvedPeriod(p, today - timedelta(days=1), today)
        case "this_week":
            return ResolvedPeriod(p, _week_start(today), tomorrow)
        case "last_week":
            ws = _week_start(today)
            return ResolvedPeriod(p, ws - timedelta(days=7), ws)
        case "this_month":
            return ResolvedPeriod(p, _month_start(today), tomorrow)
        case "last_month":
            ms = _month_start(today)
            return ResolvedPeriod(p, _add_months(ms, -1), ms)
        case "this_quarter":
            return ResolvedPeriod(p, _quarter_start(today), tomorrow)
        case "last_quarter":
            qs = _quarter_start(today)
            return ResolvedPeriod(p, _add_months(qs, -3), qs)
        case "ytd":
            return ResolvedPeriod(p, date(today.year, 1, 1), tomorrow)
        case "last_12_months":
            return ResolvedPeriod(p, _add_months(_month_start(today), -11), tomorrow)
        case "custom":
            if spec.from_ is None or spec.to is None:  # guarded by TimeSpec, kept for mypy
                raise ValueError("custom period needs from and to")
            return ResolvedPeriod(p, spec.from_, spec.to + timedelta(days=1))
    raise ValueError(f"unknown period {p!r}")  # pragma: no cover
