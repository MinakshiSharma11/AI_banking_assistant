"""Cohere adapter (cohere SDK, v2 chat). Tool calls are on the message;
require_tool -> tool_choice="REQUIRED"."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from aiplatform.llm.base import LLMResult, Message, Stopwatch, ToolCall, ToolSpec, with_retries


class CohereProvider:
    name = "cohere"

    def __init__(self, api_key: str, model: str, timeout_s: float = 30, client: Any = None) -> None:
        if client is None:
            import cohere

            client = cohere.AsyncClientV2(api_key=api_key, timeout=timeout_s)
        self.client = client
        self.model = model

    def _kw(
        self, system: str, messages: list[Message], temperature: float, max_tokens: int
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *[m.model_dump() for m in messages],
            ],
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
        kw = self._kw(system, messages, temperature, max_tokens)
        if tools:
            kw["tools"] = [{"type": "function", "function": t.model_dump()} for t in tools]
            if require_tool:
                kw["tool_choice"] = "REQUIRED"
        resp = await with_retries(lambda: self.client.chat(**kw))
        msg = resp.message
        call = None
        for tc in getattr(msg, "tool_calls", None) or []:
            raw = tc.function.arguments
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except json.JSONDecodeError:
                continue
            call = ToolCall(name=tc.function.name, args=args)
            break
        text = "".join(getattr(c, "text", "") or "" for c in (getattr(msg, "content", None) or []))
        tokens = getattr(getattr(resp, "usage", None), "tokens", None)
        return LLMResult(
            text=text or None,
            tool_call=call,
            model=self.model,
            input_tokens=int(getattr(tokens, "input_tokens", 0) or 0),
            output_tokens=int(getattr(tokens, "output_tokens", 0) or 0),
            latency_ms=sw.ms,
        )

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        async for ev in self.client.chat_stream(
            **self._kw(system, messages, temperature, max_tokens)
        ):
            if getattr(ev, "type", "") == "content-delta":
                text = ev.delta.message.content.text
                if text:
                    yield text
