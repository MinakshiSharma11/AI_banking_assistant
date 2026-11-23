"""Client for the OBDX admin APIs (call_api tool).

OBDX_ADMIN_URL set -> real HTTP. Empty (POC default) -> the mock app in-process through
httpx's ASGI transport, so the HTTP contract is exercised without a separate server.
The caller's entitlements always come from the AuthContext.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from aiplatform.gateway.auth import AuthContext
from assistants.data.agent.tools import (
    ActiveSessionsArgs,
    SessionSummaryArgs,
    UserStatusArgs,
)


@dataclass
class ApiResult:
    columns: list[str]
    rows: list[list[Any]]
    definition: str
    raw: dict[str, Any]


class ApiNotFound(LookupError):
    pass


class ObdxClient:
    def __init__(self, base_url: str | None = None, timeout_s: float | None = None) -> None:
        self.base_url = base_url if base_url is not None else os.environ.get("OBDX_ADMIN_URL", "")
        self.timeout_s = timeout_s or float(os.environ.get("OBDX_TIMEOUT_S", "10"))
        # Real OBDX: a service credential for the admin APIs, and the bank's CA for TLS.
        self.token = os.environ.get("OBDX_API_TOKEN", "")
        self.ca_bundle = os.environ.get("OBDX_CA_BUNDLE", "")

    def _client(self) -> httpx.AsyncClient:
        if self.base_url:
            return httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_s,
                verify=self.ca_bundle or True,
                headers={"Authorization": f"Bearer {self.token}"} if self.token else None,
            )
        from mocks.obdx.app import app

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://obdx.mock",
            timeout=self.timeout_s,
        )

    async def _get(self, path: str, auth: AuthContext, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"X-Entitled-Branches": ",".join(auth.branches), "X-Username": auth.username}
        async with self._client() as c:
            r = await c.get(
                path, params={k: v for k, v in params.items() if v is not None}, headers=headers
            )
        if r.status_code == 404:
            raise ApiNotFound(path)
        r.raise_for_status()
        out: dict[str, Any] = r.json()
        return out

    async def call(self, name: str, args: BaseModel, auth: AuthContext) -> ApiResult:
        if isinstance(args, ActiveSessionsArgs):
            d = await self._get("/digx/v1/admin/sessions/active", auth, {"channel": args.channel})
            if args.by_channel:
                return ApiResult(
                    ["channel", "active_sessions"],
                    [[k, v] for k, v in d["by_channel"].items()],
                    f"Active sessions right now: {d['definition']}",
                    d,
                )
            return ApiResult(
                ["active_sessions"],
                [[d["active_sessions"]]],
                f"Active sessions right now: {d['definition']}",
                d,
            )
        if isinstance(args, UserStatusArgs):
            d = await self._get(f"/digx/v1/admin/users/{args.user_id}/status", auth, {})
            keys = ["user_id", "status_label", "failed_attempts", "last_login_at", "online_now"]
            return ApiResult(
                keys, [[d[k] for k in keys]], "Digital user status from OBDX (live).", d
            )
        if isinstance(args, SessionSummaryArgs):
            d = await self._get("/digx/v1/admin/sessions/summary", auth, {"minutes": args.minutes})
            rows = [
                ["successful_logins", d["successful_logins"]],
                ["failed_logins", d["failed_logins"]],
                ["active_sessions_now", d["active_sessions"]],
            ]
            rows += [[f"failed: {k}", v] for k, v in sorted(d["failure_reasons"].items())]
            return ApiResult(
                ["measure", "value"],
                rows,
                f"OBDX login activity in the last {d['window_minutes']} minutes (live).",
                d,
            )
        raise ValueError(f"unknown API {name}")  # pragma: no cover
