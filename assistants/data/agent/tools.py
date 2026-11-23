"""The data agent's tools. The planner must call exactly one per turn.

Spec tools: build_metric_query, write_sql, call_api, ask_clarification. `decline` is an
addition (ADR-0003) so out-of-scope and personal-data requests take the `refused` route
explicitly instead of being forced into another tool.

Role gate (spec 2): it_support may use live API tools only (plus clarify/decline).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aiplatform.tools.registry import Tool, ToolRegistry
from assistants.data.semantic.loader import SemanticModel
from assistants.data.semantic.query import MetricQuery

DATA_ROLES = frozenset({"branch_manager", "regional_head", "head_office"})
ROUTES = {
    "build_metric_query": "metric",
    "write_sql": "sql",
    "call_api": "api",
    "ask_clarification": "clarify",
    "decline": "refused",
}


class WriteSqlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(max_length=8000, description="One PostgreSQL SELECT, schema-qualified tables")
    reason: str = Field(max_length=500, description="Why no metric fits")


ApiName = Literal["get_active_sessions", "get_user_status", "get_session_summary"]


class CallApiArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: ApiName
    args: dict[str, Any] = Field(
        default_factory=dict,
        description='get_active_sessions: {"channel"?: "MOBILE"|"WEB", "by_channel"?: bool}; '
        'get_user_status: {"user_id": str}; '
        'get_session_summary: {"minutes"?: 5..1440, default 60}',
    )


class ClarifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(max_length=300)
    options: list[str] = Field(
        min_length=2, max_length=4, description="Short answers the user can click"
    )


class DeclineArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # No role reason: permissions are decided only by code (registry.parse / ToolForbidden).
    reason: Literal["not_data_question", "personal_data", "raw_documents", "other"]


# Typed args for the live APIs (validated before any HTTP call)
class ActiveSessionsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["MOBILE", "WEB"] | None = None
    by_channel: bool = False


class UserStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,20}$")


class SessionSummaryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(default=60, ge=5, le=1440)


API_ARGS: dict[str, type[BaseModel]] = {
    "get_active_sessions": ActiveSessionsArgs,
    "get_user_status": UserStatusArgs,
    "get_session_summary": SessionSummaryArgs,
}


def build_registry(model: SemanticModel) -> ToolRegistry:
    return ToolRegistry(
        [
            Tool(
                "build_metric_query",
                "DEFAULT. Answer with a metric from the catalogue: metric + dimensions + filters "
                "+ period. Code compiles it to SQL.",
                MetricQuery,
                roles=DATA_ROLES,
                schema_override=model.metric_query_schema(),
            ),
            Tool(
                "write_sql",
                "Fallback only when no metric fits: one read-only PostgreSQL SELECT over the "
                "listed tables and columns.",
                WriteSqlArgs,
                roles=DATA_ROLES,
            ),
            Tool(
                "call_api",
                "Live operational state right now (active sessions, a user's lock status, "
                "recent login activity).",
                CallApiArgs,
            ),
            Tool(
                "ask_clarification",
                "Ask the user when the period or meaning is ambiguous; give 2-4 options.",
                ClarifyArgs,
            ),
            Tool(
                "decline",
                "Refuse: not about the bank's operational data, or asks for personal data, raw "
                "payloads or document contents.",
                DeclineArgs,
            ),
        ]
    )
