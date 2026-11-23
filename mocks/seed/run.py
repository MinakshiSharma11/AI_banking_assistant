"""`make seed`: (re)create the bank schemas and load synthetic data in one transaction.

Idempotent: core/obdx/atum are dropped and rebuilt; app/audit are kept (the audit
trail survives a reseed); staff users are upserted; agent_ro grants and RLS reapplied.
"""

from __future__ import annotations

import os
import sys
from time import perf_counter
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from mocks.seed import reference as ref
from mocks.seed.config import DB_DIR, load_settings
from mocks.seed.generate import COLUMNS, LOAD_ORDER, Generator, Row


def _adapt(row: Row) -> tuple[Any, ...]:
    return tuple(Jsonb(v) if isinstance(v, dict) else v for v in row)


def _copy(cur: psycopg.Cursor[Any], table: str, rows: list[Row]) -> None:
    schema, name = table.split(".")
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(schema, name), sql.SQL(", ").join(map(sql.Identifier, COLUMNS[table]))
    )
    with cur.copy(stmt) as cp:
        for row in rows:
            cp.write_row(_adapt(row))


def seed_allowed() -> bool:
    """SEED_ALLOWED=false marks a real (or shared) database: seeding drops core, obdx and
    atum, so it must never run there."""
    return os.environ.get("SEED_ALLOWED", "true").strip().lower() not in ("0", "false", "no")


def main() -> int:
    if not seed_allowed():
        print("SEED_ALLOWED=false: refusing to seed (it would drop core, obdx and atum).")
        return 2
    s = load_settings()
    now = s.now()
    t0 = perf_counter()
    print(f"Generating synthetic data: SEED={s.seed}, anchor={now.isoformat(timespec='minutes')}")
    tables = Generator(s.seed, now, s.tz).run()
    t1 = perf_counter()
    print(f"  generated in {t1 - t0:.1f}s")

    with psycopg.connect(s.admin_url) as conn, conn.cursor() as cur:
        cur.execute((DB_DIR / "01_bank_schemas.sql").read_text(encoding="utf-8"))
        cur.execute((DB_DIR / "02_platform_schemas.sql").read_text(encoding="utf-8"))
        for table in LOAD_ORDER:
            _copy(cur, table, tables[table])
            print(f"  {table:<34} {len(tables[table]):>8,} rows")
        cur.execute((DB_DIR / "03_indexes.sql").read_text(encoding="utf-8"))
        cur.executemany(
            "INSERT INTO app.staff_user (username, display_name, role, branches) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO UPDATE SET "
            "display_name = EXCLUDED.display_name, role = EXCLUDED.role, "
            "branches = EXCLUDED.branches",
            ref.STAFF_USERS,
        )
        roles_sql = (
            (DB_DIR / "04_roles_rls.sql")
            .read_text(encoding="utf-8")
            .replace("{{AGENT_RO_PASSWORD}}", sql.Literal(s.agent_password).as_string(conn))
            .replace("{{GATEWAY_RW_PASSWORD}}", sql.Literal(s.gateway_password).as_string(conn))
        )
        cur.execute(roles_sql)
    print(f"Seeded in {perf_counter() - t0:.1f}s total. Next: make sanity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
