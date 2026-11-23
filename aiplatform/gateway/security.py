"""Gateway security primitives: mock-SSO JWTs, per-user rate limiting.

Production swaps `issue_token` for the bank IdP (Entra ID / AD FS via OIDC) and keeps
`verify_token` -> AuthContext as the only way identity enters the system.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cache

import jwt

from aiplatform.gateway.auth import ROLES, AuthContext

ALGORITHM = "HS256"
DEV_SECRET = "change-me-dev-only"  # noqa: S105 - local default, warned about at start-up


@dataclass(frozen=True)
class GatewaySettings:
    jwt_secret: str
    token_ttl_s: int
    rate_per_min: int
    max_question_chars: int
    decoder_interval_s: int


@cache
def gateway_settings() -> GatewaySettings:
    from dotenv import load_dotenv

    from aiplatform.llm.config import ROOT

    load_dotenv(ROOT / ".env")
    secret = os.environ.get("JWT_SECRET", DEV_SECRET)
    if len(secret) < 16:
        raise RuntimeError("JWT_SECRET must be at least 16 characters")
    if secret == DEV_SECRET:
        logging.getLogger("bcai.gateway").warning(
            "JWT_SECRET is the development default; set a real secret outside local dev"
        )
    return GatewaySettings(
        jwt_secret=secret,
        token_ttl_s=int(os.environ.get("JWT_TTL_MIN", "480")) * 60,
        rate_per_min=int(os.environ.get("RATE_LIMIT_PER_MIN", "20")),
        max_question_chars=int(os.environ.get("MAX_QUESTION_CHARS", "2000")),
        decoder_interval_s=int(os.environ.get("DECODER_INTERVAL_S", "300")),
    )


class InvalidToken(Exception):
    pass


def issue_token(auth: AuthContext, secret: str, ttl_s: int) -> str:
    now = int(time.time())
    claims = {
        "sub": auth.username,
        "name": auth.display_name,
        "role": auth.role,
        "branches": list(auth.branches),
        "iat": now,
        "exp": now + ttl_s,
        "iss": "bcai-mock-sso",
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify_token(token: str, secret: str) -> AuthContext:
    try:
        c = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer="bcai-mock-sso",
            options={"require": ["exp", "iat", "sub", "role", "branches"]},
        )
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e
    branches = c["branches"]
    if (
        c["role"] not in ROLES
        or not isinstance(branches, list)
        or not all(isinstance(b, str) and len(b) == 3 and b.isdigit() for b in branches)
    ):
        raise InvalidToken("malformed claims")
    return AuthContext(c["sub"], c.get("name", c["sub"]), c["role"], tuple(branches))


class RateLimiter:
    """Sliding one-minute window per user (in-process, like the POC conversation store)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits[key]
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True
