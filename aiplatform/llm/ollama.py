"""Ollama adapter: the openai SDK against Ollama's OpenAI-compatible /v1 endpoint.

- require_tool -> tool_choice="required".
- Thinking off by default (reasoning_effort "none"): ~7 s instead of ~30 s per call for
  qwen3:8b on a 16 GB Mac, with the same tool choice.
- If the model returns no valid tool call, fall back once to JSON-schema structured output
  (response_format) asking for {"tool": ..., "args": {...}}; the result is flagged.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from aiplatform.llm.base import (
    LLMResult,
    Message,
    Stopwatch,
    ToolCall,
    ToolSpec,
    with_retries,
)

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_think(text: str | None) -> str | None:
    return _THINK.sub("", text).strip() if text else text


def _parse_args(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        think: bool = False,
        timeout_s: float = 120,
        client: Any = None,
        api_key: str = "ollama",
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0
            )
        self.client = client
        self.model = model
        self.think = think

    def _base(
        self, system: str, messages: list[Message], temperature: float, max_tokens: int
    ) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *[m.model_dump() for m in messages],
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not self.think:
            kw["extra_body"] = {"reasoning_effort": "none"}
        return kw

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
        kw = self._base(system, messages, temperature, max_tokens)
        names = [t.name for t in tools or []]
        if tools:
            kw["tools"] = [{"type": "function", "function": t.model_dump()} for t in tools]
            kw["tool_choice"] = "required" if require_tool else "auto"
        resp = await with_retries(lambda: self.client.chat.completions.create(**kw))
        msg = resp.choices[0].message
        call = None
        for tc in getattr(msg, "tool_calls", None) or []:
            args = _parse_args(tc.function.arguments)
            if tc.function.name in names and args is not None:
                call = ToolCall(name=tc.function.name, args=args)
                break
        tin, tout = _usage(resp)
        if call is None and require_tool and tools:
            return await self._structured_fallback(
                system, messages, tools, temperature, max_tokens, sw, tin, tout
            )
        return LLMResult(
            text=_strip_think(msg.content),
            tool_call=call,
            model=self.model,
            input_tokens=tin,
            output_tokens=tout,
            latency_ms=sw.ms,
        )

    async def _structured_fallback(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        sw: Stopwatch,
        tin: int,
        tout: int,
    ) -> LLMResult:
        catalog = "\n".join(
            f"- {t.name}: {t.description}\n  args schema: "
            f"{json.dumps(t.parameters, separators=(',', ':'))}"
            for t in tools
        )
        sys2 = (
            f'{system}\n\nReply ONLY with a JSON object {{"tool": <name>, "args": {{...}}}}'
            f" choosing exactly one of these tools:\n{catalog}"
        )
        kw = self._base(sys2, messages, temperature, max_tokens)
        kw["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "tool_call",
                "schema": {
                    "type": "object",
                    "required": ["tool", "args"],
                    "properties": {
                        "tool": {"type": "string", "enum": [t.name for t in tools]},
                        "args": {"type": "object"},
                    },
                },
            },
        }
        resp = await with_retries(lambda: self.client.chat.completions.create(**kw))
        content = _strip_think(resp.choices[0].message.content) or ""
        parsed = _parse_args(content)
        call = None
        if (
            parsed
            and parsed.get("tool") in {t.name for t in tools}
            and isinstance(parsed.get("args"), dict)
        ):
            call = ToolCall(name=parsed["tool"], args=parsed["args"])
        t2in, t2out = _usage(resp)
        return LLMResult(
            text=None if call else content,
            tool_call=call,
            model=self.model,
            input_tokens=tin + t2in,
            output_tokens=tout + t2out,
            latency_ms=sw.ms,
            fallback="structured_output",
        )

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        kw = self._base(system, messages, temperature, max_tokens)
        kw["stream"] = True
        resp = await with_retries(lambda: self.client.chat.completions.create(**kw))
        in_think = False
        async for chunk in resp:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content or ""
            if "<think>" in piece:
                in_think = True
            if in_think:
                if "</think>" in piece:
                    in_think = False
                    piece = piece.split("</think>", 1)[1]
                else:
                    continue
            if piece:
                yield piece


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        query_instruction: str = "",
        timeout_s: float = 60,
        client: Any = None,
        api_key: str = "ollama",
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0
            )
        self.client = client
        self.model = model
        self.query_instruction = query_instruction

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        inputs = [self.query_instruction + t for t in texts] if query else texts
        resp = await with_retries(
            lambda: self.client.embeddings.create(model=self.model, input=inputs)
        )
        return [list(d.embedding) for d in resp.data]


def _usage(resp: Any) -> tuple[int, int]:
    u = getattr(resp, "usage", None)
    return (int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0))
