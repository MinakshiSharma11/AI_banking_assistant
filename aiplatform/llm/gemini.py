"""Gemini adapter (google-genai SDK). Function calls arrive in response parts;
require_tool -> function-calling mode ANY. Tool schemas are passed as
`parameters_json_schema` (raw JSON schema)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aiplatform.llm.base import LLMResult, Message, Stopwatch, ToolCall, ToolSpec, with_retries

# Gemini compiles a tool schema into a decoding constraint and rejects ones with too many
# states (400 INVALID_ARGUMENT); nested array length limits (filters x values) cause it.
# Dropping them here is safe: the tool registry validates every call against the full
# Pydantic model in code, so the limits are still enforced.
_UNSUPPORTED_KEYS = frozenset({"maxItems", "minItems"})


def gemini_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: gemini_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_KEYS}
    if isinstance(schema, list):
        return [gemini_schema(v) for v in schema]
    return schema


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_s: float = 30,
        client: Any = None,
        api_version: str = "",
    ) -> None:
        from google.genai import types

        self.types = types
        if client is None:
            from google import genai

            client = genai.Client(
                api_key=api_key,
                # Preview models are served on v1beta only; empty = the SDK's default.
                http_options=types.HttpOptions(
                    timeout=int(timeout_s * 1000), api_version=api_version or None
                ),
            )
        self.client = client
        self.model = model

    def _contents(self, messages: list[Message]) -> list[Any]:
        t = self.types
        return [
            t.Content(role="user" if m.role == "user" else "model", parts=[t.Part(text=m.content)])
            for m in messages
        ]

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        require_tool: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        t = self.types
        sw = Stopwatch()
        cfg: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if tools:
            cfg["tools"] = [
                t.Tool(
                    function_declarations=[
                        t.FunctionDeclaration(
                            name=s.name,
                            description=s.description,
                            parameters_json_schema=gemini_schema(s.parameters),
                        )
                        for s in tools
                    ]
                )
            ]
            cfg["tool_config"] = t.ToolConfig(
                function_calling_config=t.FunctionCallingConfig(
                    mode="ANY" if require_tool else "AUTO"
                )
            )
        resp = await with_retries(
            lambda: self.client.aio.models.generate_content(
                model=self.model,
                contents=self._contents(messages),
                config=t.GenerateContentConfig(**cfg),
            )
        )
        call, text = None, []
        for cand in (resp.candidates or [])[:1]:
            for part in (cand.content.parts if cand.content else None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None and call is None:
                    call = ToolCall(name=fc.name, args=dict(fc.args or {}))
                elif getattr(part, "text", None):
                    text.append(part.text)
        um = resp.usage_metadata
        return LLMResult(
            text="".join(text) or None,
            tool_call=call,
            model=self.model,
            input_tokens=int(getattr(um, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(um, "candidates_token_count", 0) or 0),
            latency_ms=sw.ms,
        )

    async def stream(
        self, system: str, messages: list[Message], temperature: float = 0.2, max_tokens: int = 512
    ) -> AsyncIterator[str]:
        cfg = self.types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, max_output_tokens=max_tokens
        )
        stream = await with_retries(
            lambda: self.client.aio.models.generate_content_stream(
                model=self.model, contents=self._contents(messages), config=cfg
            )
        )
        async for chunk in stream:
            if getattr(chunk, "text", None):
                yield chunk.text
