"""In-process tool registry (POC form of the MCP tool layer).

Each tool has typed args (a Pydantic model), a description, and the roles allowed to use
it. The registry produces the per-role tool list for the LLM and validates every tool call
in code; the LLM's choice is never trusted on its own. Production: one MCP server per
backend with the same contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from aiplatform.llm.base import ToolCall, ToolSpec, inline_refs


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    roles: frozenset[str] | None = None  # None = every role
    schema_override: dict[str, object] | None = None  # e.g. enums from the semantic model

    def spec(self) -> ToolSpec:
        schema = self.schema_override or self.args_model.model_json_schema(by_alias=True)
        return ToolSpec(
            name=self.name, description=self.description, parameters=inline_refs(dict(schema))
        )


class ToolError(ValueError):
    """Invalid tool call. Message is planner-facing (used for the one retry)."""


class ToolForbidden(ToolError):
    """The tool exists but the caller's role may not use it (refuse, do not retry)."""


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = {t.name: t for t in tools}

    def allowed(self, role: str) -> list[Tool]:
        return [t for t in self.tools.values() if t.roles is None or role in t.roles]

    def specs(self, role: str) -> list[ToolSpec]:
        return [t.spec() for t in self.allowed(role)]

    def all_specs(self) -> list[ToolSpec]:
        """Every tool. Offer these to the LLM and enforce roles in `parse`: hiding tools
        makes models misuse the remaining ones, while a forbidden call is refused in code."""
        return [t.spec() for t in self.tools.values()]

    def parse(self, call: ToolCall, role: str) -> BaseModel:
        tool = self.tools.get(call.name)
        if tool is not None and tool.roles is not None and role not in tool.roles:
            raise ToolForbidden(f"Tool '{call.name}' is not allowed for role {role}.")
        if tool is None:
            raise ToolError(
                f"Tool '{call.name}' is not available. "
                f"Use one of {[t.name for t in self.allowed(role)]}."
            )
        try:
            return tool.args_model.model_validate(call.args)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5]
            )
            raise ToolError(f"Invalid arguments for {call.name}: {problems}") from e
