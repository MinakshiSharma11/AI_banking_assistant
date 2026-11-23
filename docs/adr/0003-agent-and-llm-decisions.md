# ADR-0003: Data agent and LLM gateway decisions (Phase 3)

Status: accepted (flagged for review) · 2026-11-17

1. **A fifth planner tool, `decline(reason)`.** The spec lists four tools and a `refused`
   route. Without an explicit refusal tool, local models forced into `require_tool` push
   out-of-scope or personal-data questions into another tool. Reasons:
   `not_data_question`, `personal_data`, `raw_documents`, `other`. (Amended 2026-11-17: `role_not_permitted` removed; role refusals come only from code, see ADR-0005 §11.)
   The user-facing refusal texts are fixed in code (English and Arabic), not written by the
   LLM. Security still never depends on it: the validator, grants and RLS block the same
   requests if the planner tries `write_sql` instead.
2. **Security rejections are refused, not retried.** Validator codes for blocked columns
   or tables, write statements, multiple statements and forbidden functions end the
   request with `route=refused, status=blocked`. Fixable errors (unknown metric or
   dimension, bad period, unqualified table, a DB error in LLM SQL) get the spec's single
   retry.
3. **The answer is checked before it is shown.** The answerer's stream is buffered, run
   through the grounding check, then sent as one `answer_delta`. Streaming token by token
   would show a number before the check could replace it. Cost: time to first answer
   token = full answer time (about 1–3 s on hosted models). Revisit if that misses the 4 s
   target.
4. **Chart event field is `chart_type`.** The internal event dict uses `type` for the event
   kind; the gateway maps `chart_type` back to the spec's `type` in the SSE payload.
5. **Anthropic: no temperature.** The installed SDK (anthropic 2.x) exposes no
   `temperature` for current Claude models, so the adapter does not send one. The other
   adapters use 0.0 for the planner and 0.2 for the answerer, per spec.
6. **Ollama setup.** `bcai-qwen3-8b` = qwen3:8b with an 8k context (Modelfile), because the
   ~4k-token planner prompt exceeds Ollama's 4k default and the OpenAI-compatible API
   cannot set `num_ctx`. Thinking off (`reasoning_effort: "none"`). If a tool call is
   missing or invalid, the adapter falls back once to JSON-schema structured output.
   Server: flash attention and 8-bit KV cache to cut memory.
7. **Example retrieval** uses `qwen3-embedding:0.6b` with a query-side instruction prefix
   (8/8 correct top-1 on English, Arabic and paraphrase probes vs 7/8 without it), vectors
   cached in `.cache/`, and word overlap as an automatic fallback if Ollama is down.
8. **`gateway_rw` role.** The gateway/agent process reads `app.staff_user` and inserts into
   `audit.query_log` with its own role, which cannot read, update or delete audit rows or
   touch bank data. The agent's queries still run only as `agent_ro`.
9. **PII masking before egress** applies to hosted providers only (civil IDs, mobiles,
   IBANs, card numbers, e-mails in the question and conversation). The audit row keeps the
   original question.
10. **Mock OBDX entitlements** arrive as `X-Entitled-Branches` / `X-Username` headers built
    from the AuthContext; a user outside the caller's branches is a 404, indistinguishable
    from a missing user.
