# ADR-0004: Gateway and staff UI decisions (Phase 4)

Status: accepted (flagged for review) · 2026-11-17

1. **Conversation memory is in-memory per API process** (user decision). Last 6 turns plus
   the last successful MetricQuery per conversation; 8-hour idle expiry, 2,000-conversation
   cap. Lost on restart and not shared across replicas. Open item #2 in
   `docs/design-backlog.md`.
2. **Answers are checked, then shown** (user decision; ADR-0003 §3). The UI receives the
   answer as one `answer_delta` after the grounding check. Open item #1.
3. **Mock SSO** issues HS256 JWTs (`iss=bcai-mock-sso`, 8 h) carrying role and branches from
   `app.staff_user`. Every endpoint derives the AuthContext from the token; request bodies
   reject unknown fields, so identity cannot be smuggled in. Production: bank IdP via OIDC.
4. **Conversations belong to their owner.** Another user asking for that conversation ID
   gets a 404 on history, and a new conversation on `/chat`. Feedback is accepted only for
   the caller's own messages.
5. **Rate limit and size cap** are enforced in the gateway (20/min/user, sliding window,
   in-process; 2,000 characters) and each rejection writes an audit row
   (`route=refused, status=blocked`).
6. **Feedback** is stored in `app.feedback` with the gateway role. A thumbs-up on a
   successful metric answer sets `candidate_example=true` with the question and
   MetricQuery for curators; nothing is added to `examples.yaml` automatically.
7. **SSE framing**: `event: <type>` plus a JSON `data:` line. A first `conversation` event
   returns the conversation ID (an addition to the spec's list). The chart payload's
   `type` field is restored from the agent's internal `chart_type`.
8. **The Atumverse decoder runs inside the API process** every 5 minutes
   (`DECODER_INTERVAL_S`) as a background task. `/health` reports the decode backlog and
   error count, the database and the LLM.
9. **The UI talks only to `/api`**: the Vite dev server (bound to 127.0.0.1) or nginx in
   Docker proxies it to the gateway, so no CORS is opened. nginx has proxy buffering off
   so SSE events arrive as they are sent.
10. **Docker Compose** adds `api` (seeds once on first start via `mocks.seed.ensure`, then
    uvicorn) and `web` (nginx serving the built UI). Ollama stays on the host for the Metal
    GPU and is reached via `host.docker.internal`.
11. **Layering**: the gateway lives in `aiplatform/gateway` and imports the data assistant
    directly, as the only assistant so far. When the log and document assistants arrive,
    the router chooses the assistant from the request's `assistant` field.
