"""Mock OBDX admin APIs: live operational state, same contract a real OBDX would expose.

Caller entitlements arrive in headers set by the gateway/agent (X-Entitled-Branches,
X-Username). Real OBDX would take a service token plus user context; the agent's
tool contract stays the same. Every query runs as agent_ro with RLS for those branches,
and a user outside the caller's branches is reported as not found (no existence leak).

    uvicorn mocks.obdx.app:app --port 8201     # standalone, optional
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query

from assistants.data.settings import get_settings

app = FastAPI(title="Mock OBDX admin API", version="0.1")
_BRANCH = re.compile(r"^\d{3}$")
ACTIVE_WINDOW_MIN = 15
ACTIVE_DEFINITION = (
    f"Successful OBDX sessions with no logout and activity in the last {ACTIVE_WINDOW_MIN} minutes."
)


def entitled_branches(x_entitled_branches: Annotated[str, Header()]) -> list[str]:
    branches = [b.strip() for b in x_entitled_branches.split(",") if b.strip()]
    if not branches or not all(_BRANCH.match(b) for b in branches):
        raise HTTPException(403, "missing or invalid branch entitlements")
    return branches


Branches = Annotated[list[str], Depends(entitled_branches)]


def _query(
    branches: list[str], sql: str, params: dict[str, Any] | None = None
) -> list[tuple[Any, ...]]:
    s = get_settings()
    with psycopg.connect(s.agent_url, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.user_branches', %s, true)", (",".join(branches),))
        cur.execute("SELECT set_config('TimeZone', %s, true)", (s.tz_name,))
        cur.execute(sql, params or {})
        rows = cur.fetchall()
        conn.rollback()
    return rows


def _now() -> str:
    return datetime.now(get_settings().tz).isoformat(timespec="seconds")


@app.get("/digx/v1/admin/sessions/active")
def active_sessions(
    branches: Branches, channel: Annotated[str | None, Query(pattern="^(MOBILE|WEB)$")] = None
) -> dict[str, Any]:
    return _active(branches, channel)


def _active(branches: list[str], channel: str | None = None) -> dict[str, Any]:
    rows = _query(
        branches,
        """
        SELECT channel, count(*) FROM obdx.digx_user_session
        WHERE login_status = 'S' AND logout_at IS NULL
          AND last_activity_at >= now() - make_interval(mins => %(w)s)
          AND (%(ch)s::text IS NULL OR channel = %(ch)s)
        GROUP BY channel ORDER BY channel""",
        {"w": ACTIVE_WINDOW_MIN, "ch": channel},
    )
    by_channel = {r[0]: int(r[1]) for r in rows}
    return {
        "as_of": _now(),
        "active_sessions": sum(by_channel.values()),
        "by_channel": by_channel,
        "definition": ACTIVE_DEFINITION,
    }


@app.get("/digx/v1/admin/users/{user_id}/status")
def user_status(
    branches: Branches, user_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{3,20}$")]
) -> dict[str, Any]:
    rows = _query(
        branches,
        """
        SELECT u.user_id, u.user_type, u.status, u.failed_attempts, u.registered_at,
               u.last_login_at,
               EXISTS (SELECT 1 FROM obdx.digx_user_session s WHERE s.user_id = u.user_id
                       AND s.login_status = 'S' AND s.logout_at IS NULL
                       AND s.last_activity_at >= now() - make_interval(mins => %(w)s))
        FROM obdx.digx_user u WHERE u.user_id = %(u)s""",
        {"u": user_id, "w": ACTIVE_WINDOW_MIN},
    )
    if not rows:
        raise HTTPException(404, "user not found")
    r = rows[0]
    return {
        "as_of": _now(),
        "user_id": r[0],
        "user_type": r[1],
        "status": r[2],
        "status_label": {"A": "Active", "L": "Locked", "D": "Disabled"}.get(r[2], r[2]),
        "failed_attempts": r[3],
        "registered_at": r[4].isoformat(),
        "last_login_at": r[5].isoformat() if r[5] else None,
        "online_now": bool(r[6]),
    }


@app.get("/digx/v1/admin/sessions/summary")
def session_summary(
    branches: Branches, minutes: Annotated[int, Query(ge=5, le=1440)] = 60
) -> dict[str, Any]:
    rows = _query(
        branches,
        """
        SELECT login_status, failure_reason, channel, count(*) FROM obdx.digx_user_session
        WHERE login_at >= now() - make_interval(mins => %(m)s)
        GROUP BY 1, 2, 3""",
        {"m": minutes},
    )
    ok = sum(r[3] for r in rows if r[0] == "S")
    failed = sum(r[3] for r in rows if r[0] == "F")
    reasons: dict[str, int] = {}
    channels: dict[str, int] = {}
    for status, reason, ch, n in rows:
        if status == "F":
            reasons[reason] = reasons.get(reason, 0) + int(n)
        else:
            channels[ch] = channels.get(ch, 0) + int(n)
    active = _active(branches)["active_sessions"]
    return {
        "as_of": _now(),
        "window_minutes": minutes,
        "successful_logins": int(ok),
        "failed_logins": int(failed),
        "failure_reasons": reasons,
        "successful_by_channel": channels,
        "active_sessions": active,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
