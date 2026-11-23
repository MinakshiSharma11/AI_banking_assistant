"""Ask the data agent from the terminal, without the UI.

    python -m assistants.data.cli "accounts opened by branch last quarter" --user ho_analyst
    python -m assistants.data.cli --user bm_salmiya          # conversation (follow-ups work)
    python -m assistants.data.cli "..." --json               # raw SSE-style events

The user is resolved from app.staff_user (mock SSO); the AuthContext is the only source of
identity and entitlements, exactly as the gateway will pass it in phase 4.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from aiplatform.gateway.auth import AuthContext, UnknownUser, load_staff_user
from aiplatform.llm import PROVIDERS, get_embedder, get_provider
from assistants.data.agent.agent import ConversationContext, DataAgent

DIM, BOLD, RED, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[0m"


def _table(columns: list[str], rows: list[list[Any]], limit: int = 15) -> str:
    shown = [[str(c) for c in r] for r in rows[:limit]]
    widths = [
        max(len(h), *(len(r[i]) for r in shown)) if shown else len(h) for i, h in enumerate(columns)
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(columns, widths, strict=True))
    body = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in shown]
    more = [f"{DIM}... {len(rows) - limit} more rows{RESET}"] if len(rows) > limit else []
    return "\n".join([line, "-" * len(line), *body, *more])


def _print(ev: dict[str, Any]) -> None:
    t = ev["type"]
    if t == "status":
        print(f"{DIM}[{ev['stage']}]{RESET}")
    elif t == "plan":
        print(
            f"{DIM}plan #{ev['attempt']}: {ev['route']} -> "
            f"{json.dumps(ev['args'], ensure_ascii=False)}{RESET}"
        )
    elif t == "clarify":
        print(f"{BOLD}{ev['question']}{RESET}")
        for i, o in enumerate(ev["options"], 1):
            print(f"  {i}. {o}")
    elif t == "result":
        print(_table(ev["columns"], ev["rows"]))
        if ev["truncated"]:
            print(f"{DIM}(truncated at {ev['row_count']} rows){RESET}")
    elif t == "chart":
        print(
            f"{DIM}chart: {ev['chart_type']} "
            f"{json.dumps({k: v for k, v in ev.items() if k not in ('type', 'chart_type')})}"
            f"{RESET}"
        )
    elif t == "answer_delta":
        print(f"\n{BOLD}{ev['text']}{RESET}\n")
    elif t == "definition":
        print(f"{DIM}Definition used: {ev['text']}{RESET}")
    elif t == "sql":
        print(f"{DIM}SQL:\n{ev['text']}{RESET}")
    elif t == "error":
        print(f"{RED}{ev['code']}: {ev['message']}{RESET}")
    elif t == "done":
        print(
            f"{DIM}[{ev['route']} / {ev['status']}] {ev['provider']}:{ev['model']} "
            f"{ev['latency_ms']} ms, tokens in/out {ev['tokens']['input']}/"
            f"{ev['tokens']['output']}, grounded={ev['grounded']}, "
            f"examples via {ev['retrieval']}"
            f"{', fallback=' + ev['fallback'] if ev['fallback'] else ''}{RESET}"
        )


async def _ask(
    agent: DataAgent, q: str, auth: AuthContext, ctx: ConversationContext, as_json: bool
) -> None:
    async for ev in agent.run(q, auth, ctx):
        if as_json:
            print(json.dumps(ev, ensure_ascii=False, default=str))
        else:
            _print(ev)


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("question", nargs="?", help="omit for an interactive conversation")
    p.add_argument(
        "--user", default="ho_analyst", help="bm_salmiya | rh_north | ho_analyst | it_ops"
    )
    p.add_argument("--provider", choices=PROVIDERS, help="override LLM_PROVIDER")
    p.add_argument("--json", action="store_true", help="print raw events")
    p.add_argument("--no-embeddings", action="store_true", help="word-overlap example retrieval")
    a = p.parse_args(argv)
    try:
        auth = load_staff_user(a.user)
    except UnknownUser:
        print(f"Unknown user {a.user!r}", file=sys.stderr)
        return 2
    agent = DataAgent(get_provider(a.provider), None if a.no_embeddings else get_embedder())
    ctx = ConversationContext()
    print(
        f"{DIM}{auth.display_name} ({auth.role}), branches {', '.join(auth.branches)}; "
        f"LLM {agent.provider.name}:{agent.provider.model}{RESET}"
    )
    if a.question:
        await _ask(agent, a.question, auth, ctx, a.json)
        return 0
    while True:
        try:
            q = input(f"{BOLD}> {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q in ("exit", "quit", ""):
            return 0
        await _ask(agent, q, auth, ctx, a.json)


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
