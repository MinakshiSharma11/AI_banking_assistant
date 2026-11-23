"""Provider-neutral LLM interface. Agents import only this module and `get_provider`;
vendor SDKs are imported only inside aiplatform/llm (CLAUDE.md rule)."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema, refs inlined


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class LLMResult(BaseModel):
    text: str | None
    tool_call: ToolCall | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    fallback: str | None = None  # e.g. "structured_output" when tool calling failed


class LLMError(RuntimeError):
    """Provider failed after retries. Message is safe to log (no keys, no payloads)."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        require_tool: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult: ...

    def stream(
        self,
        system: str,
        messages: list[Message],
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]: ...


class EmbeddingProvider(Protocol):
    model: str

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local $ref/$defs so every provider gets a flat JSON schema."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                merged = {
                    **walk(copy.deepcopy(target)),
                    **{k: v for k, v in node.items() if k != "$ref"},
                }
                return merged
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    out: dict[str, Any] = walk(schema)
    return out


RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def is_retryable(e: BaseException) -> bool:
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if isinstance(status, int) and status in RETRY_STATUS:
        return True
    name = type(e).__name__.lower()
    return any(
        k in name
        for k in (
            "timeout",
            "connection",
            "ratelimit",
            "overloaded",
            "serviceunavailable",
            "internalserver",
        )
    )


async def with_retries[T](
    fn: Callable[[], Awaitable[T]], retries: int = 2, base_delay: float = 0.5
) -> T:
    """Spec: 2 retries with exponential backoff on 429/5xx (and timeouts)."""
    for attempt in range(retries + 1):
        try:
            return await fn()
        except Exception as e:
            if attempt >= retries or not is_retryable(e):
                raise LLMError(f"{type(e).__name__}: {str(e)[:300]}") from e
            await asyncio.sleep(base_delay * 2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)
