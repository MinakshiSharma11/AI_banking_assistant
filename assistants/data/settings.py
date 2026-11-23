"""Data-assistant settings, read from the environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SEMANTIC_DIR = Path(__file__).resolve().parent / "semantic"


@dataclass(frozen=True)
class DataSettings:
    agent_url: str  # agent_ro: the only connection the agent uses
    decoder_url: str  # Atumverse decoder (POC: the owner role; production: its own role)
    tz_name: str
    max_rows: int  # display cap
    llm_rows: int  # rows the answerer LLM may see
    cost_ceiling: float  # EXPLAIN total-cost ceiling
    statement_timeout_s: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


@cache
def get_settings() -> DataSettings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get
    return DataSettings(
        agent_url=env(
            "AGENT_DATABASE_URL", "postgresql://agent_ro:agent_ro_dev_only@localhost:5544/bankdata"
        ),
        decoder_url=env(
            "DECODER_DATABASE_URL",
            env("ADMIN_DATABASE_URL", "postgresql://bcai:bcai_dev_only@localhost:5544/bankdata"),
        ),
        tz_name=env("BANK_TZ", "Asia/Kuwait"),
        max_rows=int(env("MAX_DISPLAY_ROWS", "500")),
        llm_rows=int(env("MAX_LLM_ROWS", "50")),
        cost_ceiling=float(env("SQL_COST_CEILING", "500000")),
        statement_timeout_s=10,
    )
