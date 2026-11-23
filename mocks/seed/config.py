"""Settings for the seeder and DB tests, read from the environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DB_DIR = ROOT / "infra" / "db"


@dataclass(frozen=True)
class SeedSettings:
    admin_url: str
    agent_url: str
    agent_password: str
    gateway_password: str
    seed: int
    tz: ZoneInfo
    seed_date: date | None

    def now(self) -> datetime:
        """Anchor for the data window: SEED_DATE at noon, else the current time."""
        if self.seed_date is None:
            return datetime.now(self.tz)
        return datetime.combine(self.seed_date, time(12), tzinfo=self.tz)


def load_settings() -> SeedSettings:
    load_dotenv(ROOT / ".env")
    raw_date = os.environ.get("SEED_DATE", "").strip()
    return SeedSettings(
        admin_url=os.environ.get(
            "ADMIN_DATABASE_URL", "postgresql://bcai:bcai_dev_only@localhost:5544/bankdata"
        ),
        agent_url=os.environ.get(
            "AGENT_DATABASE_URL",
            "postgresql://agent_ro:agent_ro_dev_only@localhost:5544/bankdata",
        ),
        agent_password=os.environ.get("AGENT_RO_PASSWORD", "agent_ro_dev_only"),
        gateway_password=os.environ.get("GATEWAY_RW_PASSWORD", "gateway_rw_dev_only"),
        seed=int(os.environ.get("SEED", "42")),
        tz=ZoneInfo(os.environ.get("BANK_TZ", "Asia/Kuwait")),
        seed_date=date.fromisoformat(raw_date) if raw_date else None,
    )
