"""`make refresh-live`: re-create the "active right now" OBDX sessions before a demo.

Seeded active sessions are relative to the seed run and age out after 15 minutes.
This replaces a reserved set of live sessions (fixed UUIDs, so repeated runs replace
rather than accumulate) with 50-150 fresh ones stamped relative to now(). Historical
data is untouched; only these rows and digx_user.last_login_at change.

    make refresh-live              # random count in 50-150
    make refresh-live COUNT=120    # exact count
"""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from datetime import datetime, timedelta

import psycopg

from mocks.seed.config import load_settings

MAX_LIVE = 150
_NS = uuid.UUID("6f1c7d2e-9a54-4c1b-8e0f-3b2a1d5c7e90")
LIVE_IDS = [uuid.uuid5(_NS, f"live-session-{k}") for k in range(MAX_LIVE)]


def live_rows(
    users: list[str], count: int, now: datetime, rng: random.Random
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for sid, user in zip(LIVE_IDS[:count], rng.sample(users, count), strict=True):
        login = now - timedelta(minutes=rng.uniform(1, 90))
        last = max(login, now - timedelta(minutes=rng.uniform(0, 14)))
        mobile = rng.random() < 0.8
        os_ = (
            rng.choices(["IOS", "ANDROID"], [45, 55])[0]
            if mobile
            else rng.choices(["WINDOWS", "MACOS"], [70, 30])[0]
        )
        rows.append((sid, user, "MOBILE" if mobile else "WEB", os_, login, last, None, "S", None))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", type=int, default=0, help="sessions to create (default 50-150)")
    args = p.parse_args(argv)
    s = load_settings()
    now = datetime.now(s.tz)
    rng = random.Random(f"{s.seed}-{now.isoformat()}")
    count = args.count or rng.randint(50, MAX_LIVE)
    if not 1 <= count <= MAX_LIVE:
        p.error(f"--count must be between 1 and {MAX_LIVE}")

    with psycopg.connect(s.admin_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM obdx.digx_user WHERE status = 'A' ORDER BY user_id")
        users = [r[0] for r in cur.fetchall()]
        if len(users) < count:
            print("Not enough active digital users; run make seed first.", file=sys.stderr)
            return 1
        cur.execute("DELETE FROM obdx.digx_user_session WHERE session_id = ANY(%s)", (LIVE_IDS,))
        rows = live_rows(users, count, now, rng)
        cur.executemany(
            "INSERT INTO obdx.digx_user_session (session_id, user_id, channel, device_os, "
            "login_at, last_activity_at, logout_at, login_status, failure_reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        cur.executemany(
            "UPDATE obdx.digx_user SET last_login_at = GREATEST(last_login_at, %s) "
            "WHERE user_id = %s",
            [(r[4], r[1]) for r in rows],
        )
        cur.execute(
            "SELECT count(*) FROM obdx.digx_user_session WHERE login_status = 'S' "
            "AND logout_at IS NULL AND last_activity_at >= now() - interval '15 minutes'"
        )
        active = cur.fetchone()
    print(
        f"Live sessions refreshed: {count} created; active right now = {active[0] if active else 0}"
        " (valid for ~15 minutes)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
