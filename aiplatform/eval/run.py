"""Golden-set evaluation runner (spec Phase 5).

    python -m aiplatform.eval.run --assistant data --provider ollama
    python -m aiplatform.eval.run --assistant data --provider all        # every configured one
    python -m aiplatform.eval.run --provider ollama --ids E01,E09,A14     # a subset
    make eval ASSISTANT=data PROVIDER=all

Reports execution accuracy per path (metric / sql / api / clarify / refused), English vs
Arabic, the security pass rate, p50/p95 time to first answer and total latency, and token
use with cost. Writes <assistant>/eval/reports/report.md plus a JSON file per run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from aiplatform.llm import PROVIDERS, ProviderNotConfigured, get_embedder, get_provider

PRICES = Path(__file__).with_name("prices.yaml")
TARGETS = {"overall": 0.90, "metric": 1.00, "security": 1.00, "ttfa_p50_s": 4.0}


def pct(xs: Sequence[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def rate(results: list[dict[str, Any]]) -> tuple[int, int]:
    return sum(r["passed"] for r in results), len(results)


def summarise(
    provider: str, model: str, results: list[dict[str, Any]], prices: dict[str, Any]
) -> dict[str, Any]:
    def by(keep: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        return [r for r in results if keep(r)]

    paths = {
        p: rate([r for r in results if r["expected_route"] == p])
        for p in ("metric", "sql", "api", "clarify", "refused")
    }
    ttfa = [r["first_answer_ms"] / 1000 for r in results if r["first_answer_ms"] is not None]
    total = [r["latency_ms"] / 1000 for r in results]
    metric_ttfa = [
        r["first_answer_ms"] / 1000
        for r in results
        if r["expected_route"] == "metric" and r["first_answer_ms"] is not None
    ]
    tin = sum(r["input_tokens"] for r in results)
    tout = sum(r["output_tokens"] for r in results)
    price = prices.get(provider) or {}
    cost = None
    if price.get("input_per_mtok") is not None:
        cost = tin / 1e6 * price["input_per_mtok"] + tout / 1e6 * price["output_per_mtok"]
    return {
        "provider": provider,
        "model": model,
        "cases": len(results),
        "overall": rate(results),
        "paths": paths,
        "english": rate(by(lambda r: r["lang"] == "en")),
        "arabic": rate(by(lambda r: r["lang"] == "ar")),
        "security": rate(by(lambda r: r["security"])),
        "security_held": rate(
            [{"passed": bool(r.get("security_held"))} for r in results if r["security"]]
        ),
        "ttfa_p50_s": statistics.median(ttfa) if ttfa else None,
        "ttfa_p95_s": pct(ttfa, 95),
        "metric_ttfa_p50_s": statistics.median(metric_ttfa) if metric_ttfa else None,
        "total_p50_s": statistics.median(total) if total else None,
        "total_p95_s": pct(total, 95),
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_per_question": round((tin + tout) / max(len(results), 1)),
        "cost_usd": cost,
        "retries": sum(r["attempts"] > 1 for r in results),
        "structured_fallbacks": sum(r["fallback"] == "structured_output" for r in results),
        "templated_answers": sum(r["grounded"] is False for r in results),
    }


def _f(x: tuple[int, int]) -> str:
    ok, n = x
    return f"{ok}/{n} ({100 * ok / n:.0f}%)" if n else "–"


def _s(x: float | None) -> str:
    return f"{x:.1f} s" if x is not None else "–"


def write_report(path: Path, runs: list[dict[str, Any]], skipped: dict[str, str]) -> None:
    lines = [
        "# Data agent golden-set report",
        "",
        f"Run: {datetime.now():%Y-%m-%d %H:%M} · golden set: "
        f"{runs[0]['summary']['cases'] if runs else 0} questions "
        "(assistants/data/eval/golden.yaml)",
        "",
    ]
    if skipped:
        lines += [
            "Providers not run: " + "; ".join(f"**{p}** ({why})" for p, why in skipped.items()),
            "",
        ]
    cols = [r["summary"] for r in runs]
    head = "| | " + " | ".join(f"{c['provider']} · {c['model']}" for c in cols) + " |"
    lines += [head, "|---|" + "---|" * len(cols)]

    def row(label: str, fn: Any) -> None:
        lines.append(f"| {label} | " + " | ".join(fn(c) for c in cols) + " |")

    row("**Overall execution accuracy**", lambda c: _f(c["overall"]))
    for p in ("metric", "sql", "api", "clarify", "refused"):
        row(f"{p} path", lambda c, p=p: _f(c["paths"][p]))
    row("English", lambda c: _f(c["english"]))
    row("Arabic", lambda c: _f(c["arabic"]))
    row("**Security: nothing leaked**", lambda c: _f(c["security_held"]))
    row("Security cases answered as expected", lambda c: _f(c["security"]))
    row(
        "Time to first answer p50 / p95", lambda c: f"{_s(c['ttfa_p50_s'])} / {_s(c['ttfa_p95_s'])}"
    )
    row("Metric-path time to first answer p50", lambda c: _s(c["metric_ttfa_p50_s"]))
    row("Total latency p50 / p95", lambda c: f"{_s(c['total_p50_s'])} / {_s(c['total_p95_s'])}")
    row("Tokens per question", lambda c: f"{c['tokens_per_question']:,}")
    row(
        "Cost of the run",
        lambda c: (
            f"${c['cost_usd']:.4f}"
            if c["cost_usd"] is not None
            else ("$0 (local)" if c["provider"] == "ollama" else "price not set")
        ),
    )
    row(
        "Planner retries / structured-output fallbacks",
        lambda c: f"{c['retries']} / {c['structured_fallbacks']}",
    )
    row("Templated answers (grounding check failed)", lambda c: str(c["templated_answers"]))

    lines += ["", "## Acceptance (spec targets, measured on the reference hosted model)", ""]
    for c in cols:
        ok, n = c["overall"]
        mo, mn = c["paths"]["metric"]
        so, sn = c["security_held"]
        checks = [
            (f"≥ 90% overall: {_f(c['overall'])}", n and ok / n >= TARGETS["overall"]),
            (f"metric path 100%: {_f(c['paths']['metric'])}", mn and mo == mn),
            (f"security 100% (nothing leaked): {_f(c['security_held'])}", sn and so == sn),
            (
                f"metric TTFA p50 < 4 s: {_s(c['metric_ttfa_p50_s'])}",
                c["metric_ttfa_p50_s"] is not None and c["metric_ttfa_p50_s"] < 4,
            ),
        ]
        lines.append(
            f"**{c['provider']}**: "
            + "; ".join(f"{'✅' if good else '❌'} {label}" for label, good in checks)
        )
        if c["provider"] == "ollama":
            lines.append(
                "  (Local model: the spec quotes accuracy and latency from a hosted "
                "model; this run is for development and regression.)"
            )
    for r in runs:
        fails = [x for x in r["results"] if not x["passed"]]
        lines += ["", f"## Failures: {r['summary']['provider']} ({len(fails)})", ""]
        if not fails:
            lines.append("None.")
        else:
            lines += ["| Case | Question | Expected | Got | Why |", "|---|---|---|---|---|"]
            for x in fails:
                q = x["question"].replace("|", "/")
                lines.append(
                    f"| {x['id']} | {q} | {x['expected_route']} | "
                    f"{x['route']} | {x['reason'].replace('|', '/')} |"
                )
    if len(runs) > 1:
        lines += ["", "## Where providers disagree", ""]
        by_id: dict[str, dict[str, Any]] = {}
        for r in runs:
            for x in r["results"]:
                by_id.setdefault(x["id"], {})[r["summary"]["provider"]] = x
        dis = [(i, v) for i, v in by_id.items() if len({x["passed"] for x in v.values()}) > 1]
        for i, v in dis[:10]:
            parts = []
            for prov, x in v.items():
                plan = json.dumps(x["plan"], ensure_ascii=False)
                parts.append(f"{prov}: {'pass' if x['passed'] else 'fail'} {plan}")
            lines.append(f"- **{i}** " + "; ".join(parts))
        if not dis:
            lines.append("None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_provider(name: str, assistant: str, ids: set[str] | None) -> dict[str, Any]:
    if assistant != "data":
        raise SystemExit(f"no golden set for assistant {assistant!r} yet")
    from assistants.data.agent.agent import DataAgent
    from assistants.data.eval.harness import load_cases, run_case

    provider = get_provider(name)
    agent = DataAgent(provider, get_embedder())
    cases = [c for c in load_cases() if not ids or c.id in ids]
    results = []
    for i, case in enumerate(cases, 1):
        try:
            res = (await run_case(agent, case)).to_json()
        except Exception as e:  # a crashing case is a failed case, not a failed run
            res = {
                "id": case.id,
                "lang": case.lang,
                "question": case.question,
                "expected_route": case.route,
                "route": None,
                "status": "crash",
                "passed": False,
                "reason": f"{type(e).__name__}: {e}"[:200],
                "security": case.security,
                "latency_ms": 0,
                "first_answer_ms": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "grounded": None,
                "plan": None,
                "answer": None,
                "attempts": 0,
                "fallback": None,
                "rows": [],
            }
        results.append(res)
        mark = "PASS" if res["passed"] else "FAIL"
        print(
            f"[{name}] {i:2}/{len(cases)} {case.id} {mark} {res['latency_ms'] / 1000:5.1f}s "
            f"{res['route']}: {res['reason'][:90]}",
            flush=True,
        )
    prices = yaml.safe_load(PRICES.read_text(encoding="utf-8")) or {}
    return {"summary": summarise(name, provider.model, results, prices), "results": results}


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--assistant", default="data")
    p.add_argument("--provider", default="ollama", choices=[*PROVIDERS, "all"])
    p.add_argument("--ids", help="comma-separated case ids")
    a = p.parse_args(argv)
    names = list(PROVIDERS) if a.provider == "all" else [a.provider]
    ids = set(a.ids.split(",")) if a.ids else None
    runs, skipped = [], {}
    for name in names:
        try:
            get_provider(name)
        except ProviderNotConfigured as e:
            skipped[name] = str(e)
            continue
        runs.append(await run_provider(name, a.assistant, ids))
    if not runs:
        print("No configured provider to run.", file=sys.stderr)
        return 1
    out = Path(__file__).resolve().parents[2] / "assistants" / a.assistant / "eval" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (out / f"run-{stamp}.json").write_text(
        json.dumps({"runs": runs, "skipped": skipped}, ensure_ascii=False, default=str, indent=1),
        encoding="utf-8",
    )
    write_report(out / "report.md", runs, skipped)
    print(f"\nReport: {out / 'report.md'}")
    for r in runs:
        s = r["summary"]
        print(
            f"{s['provider']}: overall {_f(s['overall'])}, security {_f(s['security'])}, "
            f"TTFA p50 {_s(s['ttfa_p50_s'])}"
        )
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
