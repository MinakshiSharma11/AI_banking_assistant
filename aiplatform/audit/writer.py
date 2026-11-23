"""Audit writer: one append-only audit.query_log row per request, including blocked and
failed ones. Writes with the gateway role, which can insert but never read or change rows.
Never logs API keys or result sets (only row counts)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

from aiplatform.gateway.auth import gateway_db_url

log = logging.getLogger(__name__)


@dataclass
class AuditRecord:
    username: str
    question: str
    route: str  # metric | sql | api | clarify | refused | export
    status: str  # ok | error | blocked
    user_role: str | None = None
    metric_query: dict[str, Any] | None = None
    sql_text: str | None = None
    row_count: int | None = None
    latency_ms: int | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    assistant: str = "data"
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID = field(default_factory=uuid.uuid4)


_COLS = [f for f in AuditRecord.__dataclass_fields__]
_SQL = (
    f"INSERT INTO audit.query_log ({', '.join(_COLS)}) "
    f"VALUES ({', '.join(f'%({c})s' for c in _COLS)})"
)


def write_audit(rec: AuditRecord, url: str | None = None) -> None:
    row = asdict(rec)
    row["question"] = rec.question[:4000]
    row["error"] = rec.error[:2000] if rec.error else None
    row["llm_model"] = (rec.llm_model or "")[:40] or None
    row["metric_query"] = json.dumps(rec.metric_query) if rec.metric_query is not None else None
    with psycopg.connect(url or gateway_db_url(), connect_timeout=5) as conn:
        conn.execute(_SQL, row)
