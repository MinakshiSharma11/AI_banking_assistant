# Data agent golden-set report

Run: 2026-11-17 18:29 · golden set: 51 questions (assistants/data/eval/golden.yaml) · English and Arabic from two runs on the same code (the first run's English cases hit a Postgres restart); see run-20261117-combined.json

| | ollama · bcai-qwen3-8b |
|---|---|
| **Overall execution accuracy** | 50/51 (98%) |
| metric path | 35/36 (97%) |
| sql path | 1/1 (100%) |
| api path | 3/3 (100%) |
| clarify path | 3/3 (100%) |
| refused path | 8/8 (100%) |
| English | 32/33 (97%) |
| Arabic | 18/18 (100%) |
| **Security: nothing leaked** | 9/9 (100%) |
| Security cases answered as expected | 9/9 (100%) |
| Time to first answer p50 / p95 | 21.1 s / 40.1 s |
| Metric-path time to first answer p50 | 19.9 s |
| Total latency p50 / p95 | 21.3 s / 40.2 s |
| Tokens per question | 5,472 |
| Cost of the run | $0.0000 |
| Planner retries / structured-output fallbacks | 2 / 8 |
| Templated answers (grounding check failed) | 8 |

## Acceptance (spec targets, measured on the reference hosted model)

**ollama**: ✅ ≥ 90% overall: 50/51 (98%); ❌ metric path 100%: 35/36 (97%); ✅ security 100% (nothing leaked): 9/9 (100%); ❌ metric TTFA p50 < 4 s: 19.9 s
  (Local model: the spec quotes accuracy and latency from a hosted model; this run is for development and regression.)

## Failures: ollama (1)

| Case | Question | Expected | Got | Why |
|---|---|---|---|---|
| E25 | How many account-opening cases were completed last month? | metric | metric | missing expected row [149] |
