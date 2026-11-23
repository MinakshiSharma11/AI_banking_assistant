from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mocks.seed.refresh_live import LIVE_IDS, live_rows

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=ZoneInfo("Asia/Kuwait"))


def test_live_rows_are_active_now() -> None:
    users = [f"u{i}" for i in range(300)]
    rows = live_rows(users, 120, NOW, random.Random(1))
    assert len(rows) == 120
    assert [r[0] for r in rows] == LIVE_IDS[:120]  # reserved IDs, so reruns replace
    assert len({r[1] for r in rows}) == 120  # one live session per user
    for _sid, _user, _ch, _os, login, last, logout, status, reason in rows:
        assert status == "S" and logout is None and reason is None
        assert login <= last <= NOW  # type: ignore[operator]
        assert NOW - last <= timedelta(minutes=15)  # type: ignore[operator]


def test_live_ids_are_stable() -> None:
    from mocks.seed import refresh_live

    assert refresh_live.LIVE_IDS == LIVE_IDS
    assert len(set(LIVE_IDS)) == refresh_live.MAX_LIVE
