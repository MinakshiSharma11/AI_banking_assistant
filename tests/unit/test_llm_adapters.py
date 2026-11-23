"""Provider adapters with mocked SDK clients: request mapping (tool choice, schemas) and
response normalisation to LLMResult. No network, no API keys."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any

import pytest

from aiplatform.llm import ProviderNotConfigured, get_provider
from aiplatform.llm.anthropic import AnthropicProvider
from aiplatform.llm.base import LLMError, Message, ToolCall, ToolSpec, inline_refs, with_retries
from aiplatform.llm.cohere import CohereProvider
from aiplatform.llm.gemini import GeminiProvider
from aiplatform.llm.ollama import OllamaEmbedder, OllamaProvider

TOOLS = [
    ToolSpec(
        name="build_metric_query",
        description="d",
        parameters={"type": "object", "properties": {"metric": {"type": "string"}}},
    ),
    ToolSpec(name="decline", description="d", parameters={"type": "object"}),
]
MSGS = [Message(role="user", content="accounts opened")]


class Recorder:
    """Async callable that records kwargs and returns queued responses."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kw: Any) -> Any:
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _oa_resp(content: str | None = None, tool: tuple[str, Any] | None = None) -> Any:
    calls = [NS(function=NS(name=tool[0], arguments=tool[1]))] if tool else None
    return NS(
        choices=[NS(message=NS(content=content, tool_calls=calls))],
        usage=NS(prompt_tokens=100, completion_tokens=7),
    )


# ---- Ollama (openai SDK) -------------------------------------------------------
async def test_ollama_tool_call_and_request_mapping() -> None:
    rec = Recorder(_oa_resp(tool=("build_metric_query", '{"metric": "logins"}')))
    p = OllamaProvider("http://x/v1", "m", client=NS(chat=NS(completions=NS(create=rec))))
    r = await p.complete("sys", MSGS, TOOLS, require_tool=True)
    assert r.tool_call is not None and r.tool_call.args == {"metric": "logins"}
    kw = rec.calls[0]
    assert kw["tool_choice"] == "required" and kw["temperature"] == 0.0
    assert kw["extra_body"] == {"reasoning_effort": "none"}  # thinking off
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert kw["tools"][0]["function"]["name"] == "build_metric_query"
    assert (r.input_tokens, r.output_tokens, r.fallback) == (100, 7, None)


async def test_ollama_falls_back_to_structured_output() -> None:
    rec = Recorder(
        _oa_resp(content="I think you want logins"),
        _oa_resp(content='{"tool": "build_metric_query", "args": {"metric": "x"}}'),
    )
    p = OllamaProvider("u", "m", think=True, client=NS(chat=NS(completions=NS(create=rec))))
    r = await p.complete("sys", MSGS, TOOLS, require_tool=True)
    assert r.fallback == "structured_output" and r.tool_call is not None
    assert r.tool_call.name == "build_metric_query" and r.input_tokens == 200
    second = rec.calls[1]
    assert second["response_format"]["type"] == "json_schema" and "tools" not in second
    assert "extra_body" not in second  # think=True leaves reasoning on


async def test_ollama_invalid_tool_args_and_unknown_tool_trigger_fallback() -> None:
    rec = Recorder(
        _oa_resp(tool=("build_metric_query", "{not json")),
        _oa_resp(content='{"tool": "nope", "args": {}}'),
    )
    p = OllamaProvider("u", "m", client=NS(chat=NS(completions=NS(create=rec))))
    r = await p.complete("sys", MSGS, TOOLS, require_tool=True)
    assert r.tool_call is None and r.fallback == "structured_output"


async def test_ollama_strips_think_blocks_and_streams() -> None:
    rec = Recorder(_oa_resp(content="<think>hmm</think>\nAnswer."))
    p = OllamaProvider("u", "m", client=NS(chat=NS(completions=NS(create=rec))))
    assert (await p.complete("s", MSGS)).text == "Answer."

    async def chunks() -> Any:
        for piece in ["<think>", "plan", "</think>Hello", " world", None]:
            yield NS(choices=[NS(delta=NS(content=piece))])
        yield NS(choices=[])

    p2 = OllamaProvider("u", "m", client=NS(chat=NS(completions=NS(create=Recorder(chunks())))))
    assert "".join([x async for x in p2.stream("s", MSGS)]) == "Hello world"


async def test_ollama_embedder_adds_query_instruction() -> None:
    rec = Recorder(NS(data=[NS(embedding=[1.0, 0.0])]), NS(data=[NS(embedding=[0.0, 1.0])]))
    e = OllamaEmbedder("u", "emb", query_instruction="Q: ", client=NS(embeddings=NS(create=rec)))
    assert await e.embed(["a"]) == [[1.0, 0.0]]
    await e.embed(["b"], query=True)
    assert rec.calls[0]["input"] == ["a"] and rec.calls[1]["input"] == ["Q: b"]


# ---- Anthropic -------------------------------------------------------------------
async def test_anthropic_mapping() -> None:
    resp = NS(
        content=[
            NS(type="text", text="ok "),
            NS(type="tool_use", name="decline", input={"reason": "other"}),
        ],
        usage=NS(input_tokens=11, output_tokens=3),
    )
    rec = Recorder(resp)
    p = AnthropicProvider("k", "claude-sonnet-5-5", client=NS(messages=NS(create=rec)))
    r = await p.complete("sys", MSGS, TOOLS, require_tool=True, temperature=0.0)
    kw = rec.calls[0]
    assert kw["tool_choice"] == {"type": "any"} and kw["system"] == "sys"
    assert kw["tools"][0]["input_schema"] == TOOLS[0].parameters
    assert "temperature" not in kw  # not supported by the current SDK/models (ADR-0003)
    assert r.tool_call is not None and r.tool_call.name == "decline" and r.text == "ok "
    assert (r.input_tokens, r.output_tokens) == (11, 3)


async def test_anthropic_stream() -> None:
    class Stream:
        async def __aenter__(self) -> Any:
            async def gen() -> Any:
                for t in ["a", "b"]:
                    yield t

            return NS(text_stream=gen())

        async def __aexit__(self, *a: Any) -> None:
            return None

    p = AnthropicProvider("k", "m", client=NS(messages=NS(stream=lambda **kw: Stream())))
    assert "".join([x async for x in p.stream("s", MSGS)]) == "ab"


# ---- Gemini ----------------------------------------------------------------------
async def test_gemini_mapping() -> None:
    part_call = NS(
        function_call=NS(name="build_metric_query", args={"metric": "logins"}), text=None
    )
    resp = NS(
        candidates=[NS(content=NS(parts=[NS(function_call=None, text="hi "), part_call]))],
        usage_metadata=NS(prompt_token_count=9, candidates_token_count=2),
    )
    rec = Recorder(resp)
    p = GeminiProvider("k", "gemini-flash", client=NS(aio=NS(models=NS(generate_content=rec))))
    r = await p.complete("sys", MSGS, TOOLS, require_tool=True)
    cfg = rec.calls[0]["config"]
    assert cfg.tool_config.function_calling_config.mode == "ANY"
    assert cfg.tools[0].function_declarations[0].parameters_json_schema == TOOLS[0].parameters
    assert cfg.system_instruction == "sys" and rec.calls[0]["contents"][0].role == "user"
    assert r.tool_call is not None and r.tool_call.args == {"metric": "logins"}
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi ", 9, 2)


async def test_gemini_stream() -> None:
    async def gen() -> Any:
        for t in ["x", None, "y"]:
            yield NS(text=t)

    async def start(**kw: Any) -> Any:
        return gen()

    p = GeminiProvider("k", "m", client=NS(aio=NS(models=NS(generate_content_stream=start))))
    assert "".join([x async for x in p.stream("s", MSGS)]) == "xy"


# ---- Cohere ----------------------------------------------------------------------
async def test_cohere_mapping() -> None:
    msg = NS(
        tool_calls=[NS(function=NS(name="decline", arguments=json.dumps({"reason": "x"})))],
        content=[NS(text="t")],
    )
    rec = Recorder(NS(message=msg, usage=NS(tokens=NS(input_tokens=5, output_tokens=1))))
    p = CohereProvider("k", "command", client=NS(chat=rec))
    r = await p.complete("sys", MSGS, TOOLS, require_tool=True)
    kw = rec.calls[0]
    assert kw["tool_choice"] == "REQUIRED" and kw["messages"][0]["role"] == "system"
    assert r.tool_call is not None and r.tool_call.args == {"reason": "x"} and r.text == "t"


async def test_cohere_stream() -> None:
    async def gen(**kw: Any) -> Any:
        yield NS(type="message-start")
        yield NS(type="content-delta", delta=NS(message=NS(content=NS(text="ok"))))

    p = CohereProvider("k", "m", client=NS(chat_stream=gen))
    assert [x async for x in p.stream("s", MSGS)] == ["ok"]


# ---- shared helpers ---------------------------------------------------------------
class HTTP429(Exception):
    status_code = 429


class HTTP400(Exception):
    status_code = 400


async def test_retries_on_429_then_succeeds() -> None:
    rec = Recorder(HTTP429("slow down"), HTTP429("again"), "ok")
    assert await with_retries(lambda: rec(), base_delay=0) == "ok"
    assert len(rec.calls) == 3


async def test_no_retry_on_client_errors_and_gives_up_after_two() -> None:
    rec = Recorder(HTTP400("bad"))
    with pytest.raises(LLMError, match="HTTP400"):
        await with_retries(lambda: rec(), base_delay=0)
    assert len(rec.calls) == 1
    rec2 = Recorder(TimeoutError(), TimeoutError(), TimeoutError())
    with pytest.raises(LLMError):
        await with_retries(lambda: rec2(), base_delay=0)
    assert len(rec2.calls) == 3


def test_inline_refs() -> None:
    schema = {
        "type": "object",
        "properties": {"f": {"$ref": "#/$defs/F", "description": "d"}},
        "$defs": {"F": {"type": "object", "properties": {"x": {"type": "integer"}}}},
    }
    out = inline_refs(schema)
    assert "$defs" not in out and "$ref" not in json.dumps(out)
    assert out["properties"]["f"]["properties"]["x"]["type"] == "integer"
    assert out["properties"]["f"]["description"] == "d"


def test_factory_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiplatform.llm import config

    for p, var in (
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("cohere", "COHERE_API_KEY"),
    ):
        monkeypatch.setenv(var, "")
        config.get_llm_settings.cache_clear()
        with pytest.raises(ProviderNotConfigured):
            get_provider(p)
    with pytest.raises(ProviderNotConfigured):
        get_provider("gpt-17")
    config.get_llm_settings.cache_clear()
    assert get_provider("ollama").name == "ollama"


def test_gemini_schema_drops_array_limits_but_registry_still_enforces_them() -> None:
    from aiplatform.llm.gemini import gemini_schema
    from aiplatform.tools.registry import ToolError
    from assistants.data.agent.tools import build_registry
    from assistants.data.semantic.loader import get_model

    reg = build_registry(get_model())
    spec = next(s for s in reg.all_specs() if s.name == "build_metric_query")
    sent = json.dumps(gemini_schema(spec.parameters))
    assert "maxItems" not in sent and "minItems" not in sent
    assert '"enum"' in sent  # everything else is kept
    too_many = {"metric": "accounts_opened", "dimensions": ["branch"] * 4}
    with pytest.raises(ToolError):
        reg.parse(ToolCall(name="build_metric_query", args=too_many), "head_office")


def test_gemini_api_version_is_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiplatform.llm import config

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-flash-preview")
    monkeypatch.setenv("GEMINI_API_VERSION", "v1beta")
    config.get_llm_settings.cache_clear()
    try:
        p = get_provider("gemini")
        assert p.model == "gemini-3-flash-preview"
        assert p.client._api_client._http_options.api_version == "v1beta"  # type: ignore[attr-defined]
        monkeypatch.setenv("GEMINI_API_VERSION", "")
        config.get_llm_settings.cache_clear()
        assert get_provider("gemini").client._api_client._http_options.api_version != "v1beta"  # type: ignore[attr-defined]
    finally:
        config.get_llm_settings.cache_clear()


def test_switching_provider_is_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiplatform.llm import config

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config.get_llm_settings.cache_clear()
    try:
        p = get_provider()
        assert p.name == "anthropic" and p.model == "claude-sonnet-5-5"
    finally:
        config.get_llm_settings.cache_clear()
