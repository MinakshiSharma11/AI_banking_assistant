# Bank Conversational AI

Read docs/DESIGN.md before any task. Read the spec in docs/specs/ for the assistant
you are working on. Repo layout and platform rules come from DESIGN.md; assistant
details come from the spec. If they conflict, stop and ask. Decisions already taken
are in docs/adr/.

## Non-negotiables
- The LLM chooses tools and phrases answers. Security, validation, limits and
  execution are deterministic code. Never move a control into a prompt.
- Identity comes only from the gateway AuthContext. Every tool call runs with the
  caller's entitlements.
- New backends are added as tools in aiplatform/tools with typed args, never as raw
  DB or HTTP access from an agent.
- No PII to the LLM: blocked columns, masked logs, banded values.
- The banking assistant can only prepare write actions. Execution happens in the
  confirm flow after customer confirmation and step-up auth.
- Internal and customer assistants never share a process or a tool registry instance.
- Provider SDKs are imported only inside aiplatform/llm.
- Every turn and tool call writes an audit record.

## Data agent rules (docs/specs/data-agent.md)
1. The LLM never writes SQL on the default path; it emits a MetricQuery and the
   semantic compiler builds SQL with sqlglot expressions.
2. Free SQL (write_sql) is a fallback only and always passes the validator.
3. Every SQL statement goes through the validator: SELECT-only, single statement,
   allowlist from model.yaml, PII blocked, row security injected, EXPLAIN cost cap, LIMIT 500.
4. Row security three ways: compiler predicate, validator wrapper, Postgres RLS
   (current_setting('app.user_branches')). The agent runs as agent_ro.
5. Identity only from the gateway AuthContext (JWT), never from the prompt.
6. Chart type chosen in code (KPI / line / bar / table), not by the LLM.
7. Grounding check: every number in the answer must appear in the result rows.
8. Business definitions live only in semantic/*.yaml.
9. Switching LLM_PROVIDER (anthropic | gemini | cohere | ollama) needs no code change.
10. Every request writes one audit.query_log row, including failures and refusals.
11. Atumverse payload columns, document BLOBs and ocr_json are never queryable;
    payload fields reach the agent only through atum.wf_instance_flat.

## Working agreement
- Work in the phases the spec defines. Before coding a phase, post a short plan and
  list open questions. Stop after each phase for review.
- Every change ships with tests. Keep ruff, mypy and pytest green.
- A new metric, tool or prompt change needs a golden-set entry. Run eval before
  saying a phase is done, and report the numbers.
- Use synthetic data only. Never paste real customer data into code, tests or prompts.
- Record any decision that changes DESIGN.md as an ADR in docs/adr/ and flag it.

## Commands
- make up / make down          # docker compose (Postgres on host port 5544)
- make seed                    # (re)create schemas + synthetic data + decode, idempotent
- make decode                  # Atumverse decoder (incremental)
- make refresh-live [COUNT=n]  # re-stamp 'active right now' sessions before a demo
- make sanity                  # row counts and quirk percentages
- make ollama-model            # local LLM + embedding model (bcai-qwen3-8b, 8k context)
- make ask Q="..." USER=ho_analyst   # data agent from the terminal
- make test-live               # real Ollama calls (slow)
- make api / make web          # gateway :8200, UI :5180 (dev)
- make up-all                  # db + api + web in Docker
- make test / make lint / make cov (>= 85% on assistants/data/semantic)
- make eval ASSISTANT=data PROVIDER=ollama|all [IDS=E01,A14]   # golden set -> reports/report.md

## Local ports
Postgres 5544, API 8200 (phase 4), web 5180 (phase 4), Ollama 11434.
