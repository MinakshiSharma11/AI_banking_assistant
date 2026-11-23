from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from assistants.data.semantic.periods import resolve_period
from assistants.data.semantic.query import TimeSpec

TODAY = date(2026, 11, 17)  # a Tuesday


@pytest.mark.parametrize(
    "period,start,end",
    [
        ("today", date(2026, 11, 17), date(2026, 11, 18)),
        ("yesterday", date(2026, 11, 16), date(2026, 11, 17)),
        ("this_week", date(2026, 11, 15), date(2026, 11, 18)),  # Sunday start
        ("last_week", date(2026, 11, 8), date(2026, 11, 15)),
        ("this_month", date(2026, 11, 1), date(2026, 11, 18)),
        ("last_month", date(2026, 10, 1), date(2026, 11, 1)),
        ("this_quarter", date(2026, 10, 1), date(2026, 11, 18)),
        ("last_quarter", date(2026, 7, 1), date(2026, 10, 1)),
        ("ytd", date(2026, 1, 1), date(2026, 11, 18)),
        ("last_12_months", date(2025, 12, 1), date(2026, 11, 18)),
    ],
)
def test_periods(period: str, start: date, end: date) -> None:
    r = resolve_period(TimeSpec.model_validate({"period": period}), TODAY)
    assert (r.start, r.end) == (start, end)


def test_year_boundaries() -> None:
    jan = date(2026, 1, 10)
    assert resolve_period(TimeSpec(period="last_month"), jan).start == date(2025, 12, 1)
    assert resolve_period(TimeSpec(period="last_quarter"), jan).start == date(2025, 10, 1)
    assert resolve_period(TimeSpec(period="last_quarter"), jan).end == date(2026, 1, 1)


def test_sunday_is_its_own_week_start() -> None:
    sunday = date(2026, 11, 15)
    assert resolve_period(TimeSpec(period="this_week"), sunday).start == sunday


def test_custom_is_inclusive() -> None:
    r = resolve_period(
        TimeSpec.model_validate({"period": "custom", "from": "2026-04-01", "to": "2026-06-30"}),
        TODAY,
    )
    assert (r.start, r.end, r.last_day) == (date(2026, 4, 1), date(2026, 7, 1), date(2026, 6, 30))
    assert r.label() == "custom (1 Apr 2026 – 30 Jun 2026)"


def test_custom_needs_valid_range() -> None:
    with pytest.raises(ValidationError):
        TimeSpec.model_validate({"period": "custom", "from": "2026-04-01"})
    with pytest.raises(ValidationError):
        TimeSpec.model_validate({"period": "custom", "from": "2026-05-01", "to": "2026-04-01"})


def test_bounds_for_timestamps_are_bank_local_midnight() -> None:
    tz = ZoneInfo("Asia/Kuwait")
    r = resolve_period(TimeSpec(period="yesterday"), TODAY)
    lo, hi = r.bounds("timestamp", tz)
    assert lo == datetime(2026, 11, 16, tzinfo=tz) and hi == datetime(2026, 11, 17, tzinfo=tz)
    assert r.bounds("date", tz) == (date(2026, 11, 16), date(2026, 11, 17))
    assert r.label() == "yesterday (16 Nov 2026)"
