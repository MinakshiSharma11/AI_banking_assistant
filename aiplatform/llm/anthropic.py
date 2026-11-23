"""Anthropic adapter (Claude). Tool calls arrive as `tool_use` content blocks;
require_tool -> tool_choice={"type": "any"}.

The installed SDK (anthropic 2.x) exposes no `temperature` parameter for current Claude
models, so the planner/answerer temperatures are not sent (ADR-0003).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aiplatform.llm.base import LLMResult, Message, Stopwatch, ToolCall, ToolSpec, with_retries


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_s: float = 30, client: Any = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self.client = client
        self.model = model

    def _kw(self, system: str, messages: list[Message], max_tokens: int) -> dict[str, Any]:
        return {
            "model": self.model,
            "system": system,
            "max_tokens": max_tokens,
            "messages": [m.model_dump() for m in messages],
        }

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        require_tool: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        sw = Stopwatch()
        kw = self._kw(system, messages, max_tokens)
        if tools:
            kw["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            kw["tool_choice"] = {"type": "any"} if require_tool else {"type": "auto"}
        resp = await with_retries(lambda: self.client.messages.create(**kw))
        text, call = [], None
        for block in resp.content:
            if block.type == "tool_use" and call is None:
                call = ToolCall(name=block.name, args=dict(block.input or {}))
            elif block.type == "text":
                text.append(block.text)
        return LLMResult(
            text="".join(text) or None,
            tool_call=call,
            model=self.model,
            input_tokens=int(resp.usage.input_tokens or 0),
            output_tokens=int(resp.usage.output_tokens or 0),
            latency_ms=sw.ms,
        )

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        async with self.client.messages.stream(**self._kw(system, messages, max_tokens)) as s:
            async for piece in s.text_stream:
                yield piece
