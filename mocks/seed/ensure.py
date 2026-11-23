"""Seed once: used by the API container at start-up (`docker compose up`).

Runs the full seed + decode only when the bank schemas are missing, so restarting the
stack keeps the data (and the audit trail). `make seed` always reseeds.
"""

from __future__ import annotations

import sys

import psycopg

from mocks.seed import run
from mocks.seed.config import load_settings


def seeded() -> bool:
    with psycopg.connect(load_settings().admin_url, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT to_regclass('core.cust_account') IS NOT NULL "
            "AND to_regclass('app.feedback') IS NOT NULL"
        ).fetchone()
    return bool(row and row[0])


def main() -> int:
    if not run.seed_allowed():
        print("SEED_ALLOWED=false: not seeding; using the configured database as is.")
        return 0
    if seeded():
        print("Database already seeded.")
        return 0
    code = run.main()
    if code == 0:
        from assistants.data.semantic.atum_decoder import main as decode

        code = decode()
    return code


if __name__ == "__main__":
    sys.exit(main())
