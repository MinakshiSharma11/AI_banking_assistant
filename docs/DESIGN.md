# Bank Conversational AI — Solution Design

Nov 17, 2026 · Himanshu Tewari · Markdown export of the source document. Repo deviations are recorded in `docs/adr/`.

## Target state

The bank gets one conversational AI platform with four assistants on a shared core. Three serve staff inside the bank, and one serves customers in a separately deployed, more tightly controlled zone. This document is the top-level design, and it is the first file Claude Code reads. Each assistant then gets its own detailed spec. The first is `docs/specs/data-agent.md`.

| Assistant | Users | Answers or does | Main sources | Risk class |
|---|---|---|---|---|
| Data assistant | Branch managers, ops, MIS, head office | Counts, trends and breakdowns, e.g. accounts opened by branch, active sessions, stage SLAs; any question the data model can answer, not a fixed menu | Core (FLEXCUBE-style), OBDX, Atumverse via semantic layer; OBDX/Atumverse admin APIs | Internal, read-only |
| Log assistant | L2/L3 support, DevOps | What failed, when, how often, likely cause and known fix for application errors and warnings | Central log store (OBDX, Atumverse, WebLogic, integration hub), known-errors knowledge base | Internal, read-only |
| Document assistant | All staff | Answers with citations from policies, procedures, product manuals, circulars | Document repository (DMS / SharePoint) | Internal, read-only, permission-filtered |
| Banking assistant | Retail customers on app, web, WhatsApp | Balances, transactions, card status; then transfers, bill payments, card block | OBDX APIs via the integration hub | Customer-facing, read + write |

Design principles that apply to every assistant:

1. **The LLM decides, code enforces.** The model chooses which tool to call and how to phrase the answer. Access control, validation, limits and execution are deterministic code.
2. **The user's identity flows end to end.** Every tool call runs with the caller's own entitlements, never with a shared superuser.
3. **Tools, not raw access.** Each backend is exposed as a narrow, typed tool (an MCP server). Agents never get open database or file-system access.
4. **Grounded answers.** Every answer shows where it came from: the SQL and metric definition, log lines, document citations or the API response.
5. **Model-agnostic.** Any supported LLM (Claude Sonnet 5.5, Gemini Flash, Cohere Command, a local open-weight model via Ollama, or an in-region model) is a config change behind the LLM gateway.
6. **Measured before shipped.** Each assistant has a golden test set, and no release goes live below its accuracy and security bar.
7. **Internal and customer zones never share runtime.** The banking assistant has its own deployment, network path, model policy and penetration test.

## Solution architecture

Each request goes down one column: user, gateway, assistant, then that assistant's tools. Every assistant draws on the same platform below.

```
 Business users      Tech support        All staff          |  Customers
 (branch, ops, MIS)  (L2/L3, DevOps)     (policies)         |  (app, web, WhatsApp)
        \                 |                 /               |        |
   ┌──────── Assistant gateway (internal) ─────────┐        |  Customer gateway (session, OTP)
   │ SSO, role + branch entitlements, router,      │        |        |
   │ rate limits, audit                            │        |  Banking assistant (reads, prepares)
   └───────────────────────────────────────────────┘        |        |
   Data assistant     Log assistant     Docs assistant      |  OBDX APIs via integration hub
   (metrics+safe SQL) (search,correlate)(hybrid RAG + ACL)  |
   Semantic layer     Log store         Search index        |
   (replica+admin API)(masked logs, KB) (DMS docs, ACLs)    |
   ─────────────── Internal zone ───────────────            ─── Customer zone ───
   ══════════ Shared platform: LLM gateway (Claude, Gemini, Cohere, Ollama, in-region) ·
              MCP tool layer · guardrails · audit · evaluation ══════════
```

The three staff assistants share one gateway in the internal zone. The banking assistant has its own gateway, runtime and tool registry, so nothing customer-facing can reach an internal tool.

## Shared platform components

The four assistants share one set of platform services. Each assistant is mostly configuration: a system prompt, a tool list, a golden set and a UI surface.

| Component | Responsibility | POC implementation | Production direction |
|---|---|---|---|
| Assistant gateway (internal) | SSO, JWT validation, role and entitlement lookup, conversation state, rate limits, SSE streaming | FastAPI, mock SSO with 4 demo users | Bank IdP (Entra ID / AD FS via OIDC), entitlements from the core user-role matrix |
| Customer gateway | Channel session validation, step-up auth, device binding, abuse controls | Mock OBDX session token, mock OTP | OBDX session and OTP/biometric services, WAF, bot detection |
| Router | Picks the assistant and its tool set for a message; handles "not my scope" | Explicit assistant choice in the UI, plus an LLM intent check | Same, with a cross-assistant handoff (e.g. data → docs for a definition) |
| Agent runtime | Runs the plan → tool → observe → answer loop, with a step cap and timeouts | Python package, max 4 tool calls per turn | Same, horizontally scaled |
| LLM gateway | One interface over providers; model per assistant; retries, quotas, token accounting; PII masking before egress | `llm` adapters: Anthropic, Gemini, Cohere, and Ollama for free local open-weight models (the default for development) | Add an in-region or on-prem model option for residency-bound data; the same open-weight models served with vLLM on bank GPUs |
| Tool layer (MCP) | Each backend as an MCP server with typed tools, auth passthrough, its own allowlist | In-process tool registry with the same interface | Separate MCP servers per backend, deployed next to the system they front |
| Guardrails | Input checks (injection, scope), output checks (grounding, PII, policy), tool-argument validation | Validator + grounding check + PII column blocks | Add a policy model or rules engine for the customer zone |
| Audit store | One immutable row per turn and per tool call: user, question, tool, arguments, result size, model, latency, outcome | `audit.query_log` in PostgreSQL | Append-only store, shipped to the bank's SIEM |
| Evaluation harness | Golden sets per assistant, run per provider and per release; regression gate in CI | `eval/run.py` | Nightly runs, dashboards, drift alerts |
| Observability | Traces across gateway → agent → LLM → tool; latency, errors, cost per assistant | Structured JSON logs | OpenTelemetry into the bank's APM |
| Feedback | Thumbs up/down and comments, routed to curators | `POST /feedback` | Curation queue; approved items become verified examples |

The tool layer is where reuse pays off. A tool built once, such as `search_logs` or `get_account_balance`, is available to any assistant whose role policy allows it.

## Assistant designs

All four assistants run the same loop: route, plan with tools, execute in code, answer with evidence. They differ in their tools, their data and the rules around them.

### Data assistant

It answers ad-hoc business questions over core, OBDX and Atumverse data. The LLM picks a metric query over a semantic layer, or writes SQL as a fallback, and code compiles, validates, secures and runs it. The full spec is in `docs/specs/data-agent.md`.

| Tool | Purpose |
|---|---|
| `build_metric_query` | Default path: metric + dimensions + filters + period, compiled to SQL by code |
| `write_sql` | Fallback for questions no metric covers; always validated |
| `call_api` | Live operational state: active sessions, user lock status, workflow queue now |
| `ask_clarification` | Ambiguous period, metric meaning or scope |

Key rules: queries run on a reporting replica as a read-only role, with row security injected for the user's branches. PII columns and Atumverse payload/BLOB columns are never queryable. Chart type is picked by code.

### Log assistant

It answers "what's failing and why" by querying a central log store through narrow tools. It never reads raw log files into context.

| Tool | Purpose |
|---|---|
| `search_logs` | Filter by service, level, time range, text, error code, correlation ID; returns at most 50 events |
| `aggregate_errors` | Counts by error code, service, host or minute, for spotting spikes |
| `get_trace` | All events for one correlation ID across OBDX → integration hub → core |
| `get_stack_trace` | Full stack for one event ID |
| `lookup_known_error` | Matches an error code or message signature to the known-errors base: cause, fix, last seen, ticket refs |
| `get_deployments` | Releases and config changes in a time window, to correlate failures with changes |

Key rules: PII (account numbers, civil IDs, names, mobile numbers) is masked in the log pipeline at ingest, before any LLM sees it. Without an explicit time window the default is the last 60 minutes. Answers cite event IDs and counts. Version 1 has no write actions: it can't restart services, change configs or close tickets. The known-errors base is a curated YAML/Markdown repository, built from past incidents and vendor service-request history, that the support team owns.

For the POC: a single-node OpenSearch in Docker, seeded with 7 days of synthetic logs for OBDX, Atumverse, WebLogic and the integration hub. The seed includes deliberate incidents: a DB connection-pool exhaustion spike, a certificate expiry, a timeout storm on one core API, and a release that introduced a null-pointer error.

### Document assistant

It answers staff questions from the bank's documents, with citations, and returns only passages the user is allowed to see.

| Stage | Design |
|---|---|
| Ingestion | Connectors for DMS / SharePoint; parse PDF, DOCX, PPTX, HTML; OCR for scanned PDFs; metadata: title, owner, version, effective date, language, ACL groups |
| Chunking | Split by headings, 500–800 tokens with overlap; each chunk keeps its section path and page number |
| Index | Hybrid: keyword (BM25) + vector (multilingual embeddings) in OpenSearch, reusing the log assistant's cluster in the POC |
| Retrieval | ACL filter first, then hybrid search, then a reranker; top 6–8 passages |
| Answer | Answer only from the passages, cite each claim (doc, section, page), and say "not found in the documents" when they don't cover it |

Tools: `search_documents(query, filters)`, `get_document_section(doc_id, section)`, `list_document_versions(doc_id)`.

Key rules: the ACL filter is applied in the search query, never after generation. The latest effective version wins, and superseded documents are labelled as such. Arabic and English documents are both searchable, with a separate Arabic test set. The POC uses a synthetic corpus of about 40 documents (policies, product sheets, circulars, procedure manuals) with three ACL groups.

### Banking assistant (customer)

It serves customers on app, web and WhatsApp. Reads go straight to OBDX APIs under the customer's own session. Writes are prepared by the LLM but confirmed and executed by a deterministic flow (next section).

| Tool | Type | Purpose |
|---|---|---|
| `get_accounts`, `get_balance` | Read | Customer's own accounts only |
| `get_transactions` | Read | Date range, amount, merchant/narration filters; max 50 rows to the LLM |
| `get_card_status`, `get_beneficiaries` | Read | Cards and saved payees |
| `prepare_transfer` | Write (prepare) | Own-account or saved-beneficiary transfer; returns a draft, never executes |
| `prepare_bill_payment` | Write (prepare) | Saved billers only |
| `prepare_card_block` | Write (prepare) | Temporary or permanent block, with reason |
| `handoff_to_agent` | Escalation | Live chat with a human, with the conversation summary attached |

Key rules:

- The LLM never calls an execute endpoint. Execution happens only after the customer confirms on a card rendered by the channel, plus step-up auth.
- Amounts and beneficiaries must be stated by the customer and echoed back exactly. Version 1 doesn't add new beneficiaries through chat.
- Transaction narrations and merchant names are treated as data, never as instructions (prompt-injection defence).
- No investment or credit advice. Product questions are answered from approved product content only (the document assistant's public corpus).
- The assistant hands off to a human on request, after two failed attempts at the same task, or on complaints and fraud reports.

## Customer transaction flow

A payment needs three independent yeses: the rules service, the customer's explicit confirmation, and step-up authentication. The LLM can't produce any of them.

1. **Customer** asks: "Send 250 KWD to Ahmed".
2. **LLM** prepares: `prepare_transfer(amount, saved payee)` — the only LLM step.
3. **Code** — rules service validates own account, saved payee, limits, balance. On failure: explain and stop, no draft created.
4. **Channel** renders a confirmation card: exact amount, payee, fee, draft ID.
5. **Customer** confirms: tap + OTP or biometric. If not, the draft expires after 5 minutes.
6. **Code** — confirm flow executes the OBDX transfer API with an idempotency key.
7. **Code** — result and audit: reference shown; every step logged.

The draft is held server-side and keyed by an idempotency key, so a retry or a replayed confirmation can't pay twice. Card block uses the same flow without the OTP step, because blocking a card only reduces risk.

## Security, compliance and non-functional requirements

The main risks are data leaking across entitlements, prompt injection steering a tool, and customer data leaving the jurisdiction. Each is handled by a control that doesn't depend on the model behaving well.

| Threat | Control (enforced in code or infrastructure) |
|---|---|
| User sees data outside their entitlement | Identity from the gateway JWT only; row security in compiler + validator + DB RLS; ACL filter inside document search; customer tools scoped to the session's customer ID |
| Prompt injection via question, document, log line or transaction narration | Tool results passed as delimited data; tool arguments schema-validated; write actions need out-of-band confirmation; allowlisted tools per assistant |
| SQL abuse | SELECT-only parser, table/column/function allowlist, read-only role, timeout, EXPLAIN cost cap |
| PII to the LLM | PII columns blocked; log PII masked at ingest; payload salary banded; LLM gateway masking for free text before egress |
| Unauthorised payment | LLM can only prepare; customer confirmation + step-up OTP/biometric; limits and beneficiary rules in the payment service, not the prompt |
| Data residency | Model per assistant set in the LLM gateway; in-region or on-prem model for any zone the regulator requires; no prompts or results stored by the provider (zero-retention terms) |
| Hallucinated facts | Grounding checks: numbers must match result rows; document answers must cite retrieved passages; "not found" when unsupported |
| Abuse and cost blow-out | Per-user rate limits, per-assistant token quotas, max tool steps per turn |

Compliance artefacts to plan for: a model risk assessment per assistant, a data-flow diagram per assistant for the regulator and DPO, the audit trail with retention aligned to the bank's record-keeping policy, and a pen test of the customer zone before go-live. Regulator specifics (for example CBK, CBUAE or SAMA outsourcing and cloud rules) are confirmed per client. They are not assumed in this design.

Non-functional targets (POC values; production values to be agreed per bank):

| Requirement | Internal assistants | Banking assistant |
|---|---|---|
| Time to first token, p50 | < 4 s | < 2 s |
| Full answer, p95 | < 15 s | < 6 s |
| Availability | Business hours, 99.5% | 24×7, 99.9% |
| Concurrent users (POC sizing) | 50 | 200 |
| Languages | English, Arabic | English, Arabic (Gulf colloquial in v2) |
| Golden-set accuracy to release | ≥ 90% | ≥ 95% reads; 100% on write-safety cases |
| Audit coverage | 100% of turns and tool calls | 100% of turns, tool calls and confirmations |

## Deployment and roadmap

The POC runs on one laptop with `docker compose`. Production splits into an internal zone and a customer zone on the bank's container platform, in the bank's data centre or in-region cloud.

| Environment | Runs | Data | LLM |
|---|---|---|---|
| POC (local) | docker compose: postgres, opensearch, api, web | Synthetic only | Any of the providers via API keys; Ollama by default |
| Bank DEV / SIT | Kubernetes or OpenShift namespaces per zone | Masked copies of reporting replica, logs, a document subset | Bank-approved provider or in-region model |
| UAT | Same as production topology | Masked production-like data | As production |
| Production | Internal zone (data, log, docs assistants) and customer zone (banking assistant) in separate clusters or namespaces, with separate network policies | Reporting replica, log store, DMS, OBDX APIs via integration hub | Per assistant, per residency decision |

Phased roadmap. Each phase ends with a gate that must pass before the next one starts:

1. **Platform + data assistant POC.** Shared gateway, LLM gateway, tool layer, audit, eval harness, and the data assistant on synthetic data. Gate: ≥ 90% golden-set accuracy and 100% security cases blocked, on at least two providers.
2. **Log + document assistants.** OpenSearch, log pipeline with PII masking, document ingestion with ACLs. Gate: each at ≥ 90% on its golden set; ACL leak tests at 0 failures.
3. **Internal pilot at a bank.** Connect to the reporting replica, real logs and a DMS subset; 20–50 pilot users. Gate: user satisfaction ≥ 4/5, no security findings above medium, and the business sign-off on metric definitions.
4. **Banking assistant, read-only.** Customer gateway, OBDX read tools, handoff to human. Gate: pen test passed, ≥ 95% read accuracy, regulator notification where required.
5. **Banking assistant, transactions.** Prepare/confirm/execute flow for transfers, bill payments and card block. Gate: 100% on write-safety cases, fraud and limits review, staged rollout to 5% → 25% → 100% of users.

## Guidance for Claude Code

This document lives in the repo as `docs/DESIGN.md` and each assistant's detailed spec under `docs/specs/`. Claude Code works from three layers: `CLAUDE.md` (rules), `DESIGN.md` (the target), and one spec per build (the details).

The repo is one monorepo for the whole solution, with the platform shared and each assistant in its own package:

```
bank-conversational-ai/
  CLAUDE.md
  docs/
    DESIGN.md                  # this document
    specs/
      data-agent.md            # Bank Data Agent guide (first build)
      log-agent.md             # to be written before phase 2
      docs-agent.md            # to be written before phase 2
      banking-agent.md         # to be written before phase 4
    adr/                       # architecture decision records, one per decision
  platform/                    # → aiplatform/ in this repo, see ADR-0001
    gateway/                   # auth, entitlements, router, SSE, rate limits
    llm/                       # provider interface + adapters
    tools/                     # tool registry, MCP server base, arg validation
    guardrails/                # validators, grounding checks, PII masking
    audit/                     # audit writer, schema
    eval/                      # golden-set runner, reports
  assistants/
    data/                      # semantic layer, compiler, data tools, prompts, golden set
    logs/                      # log tools, known-errors base, prompts, golden set
    docs/                      # ingestion, retrieval tools, prompts, golden set
    banking/                   # customer tools, confirm/execute flow, prompts, golden set
  mocks/
    obdx/                      # mock OBDX admin + customer APIs
    seed/                      # synthetic data: db, logs, documents
  web/
    staff/                     # internal assistant UI
    customer/                  # customer chat widget (phase 4)
  infra/
    docker-compose.yml
    k8s/                       # later phases
```

Where `docs/specs/data-agent.md` shows a different folder layout, this layout wins: its `api/semantic`, `api/agent` and `api/llm` folders map to `assistants/data`, `platform/` and `platform/llm`.

## Open decisions

None of these block the POC, which runs on synthetic data and API-hosted or local models. Each one must be settled before the phase listed.

| Decision | Options | Needed before |
|---|---|---|
| LLM per assistant and residency | Claude Sonnet 5.5, Gemini Flash, Cohere Command via API; in-region hosted; on-prem open-weight | Phase 3 (pilot) |
| Orchestration core | Custom (as in the POC), Profinch Pints agentic platform, or a framework | Phase 3 |
| Customer channel layer | Own widget + WhatsApp provider, or Oracle Digital Assistant for dialog and channels | Phase 4 |
| Atumverse payload storage | JSON, BLOB or raw text in the real product (decides whether the decoder or DB views are used) | Phase 3 |
| Log store at the bank | Reuse the bank's Splunk / ELK / Loki via tools, or deploy OpenSearch | Phase 3 |
| DMS integration | SharePoint, OpenText, other; how ACL groups are exposed | Phase 3 |
| Metric ownership | Which business owner signs off each metric definition | Phase 3 |
| Known-errors base ownership | Support team curation process and tooling | Phase 2 |
| Write scope for v1 | Transfers to saved beneficiaries only, or also new beneficiaries with extra controls | Phase 5 |
