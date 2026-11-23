"""AuthContext: the only source of identity for agents and tools.

Phase 4 builds it from a validated JWT. Agents never read identity from the prompt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache

import psycopg
from dotenv import load_dotenv

from aiplatform.llm.config import ROOT

ROLES = frozenset({"branch_manager", "regional_head", "head_office", "it_support"})


@dataclass(frozen=True)
class AuthContext:
    username: str
    display_name: str
    role: str
    branches: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if not self.branches:
            raise ValueError("a user needs at least one entitled branch")


class UnknownUser(LookupError):
    pass


@cache
def gateway_db_url() -> str:
    load_dotenv(ROOT / ".env")
    return os.environ.get(
        "GATEWAY_DATABASE_URL",
        "postgresql://gateway_rw:gateway_rw_dev_only@localhost:5544/bankdata",
    )


def load_staff_user(username: str) -> AuthContext:
    """Mock SSO lookup (app.staff_user), read with the gateway role."""
    with psycopg.connect(gateway_db_url(), connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT username, display_name, role, branches FROM app.staff_user WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        raise UnknownUser(username)
    return AuthContext(row[0], row[1], row[2], tuple(b.strip() for b in row[3]))
