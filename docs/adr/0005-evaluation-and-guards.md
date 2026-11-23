# ADR-0005: Evaluation method and the guards it led to (Phase 5)

Status: accepted (flagged for review) · 2026-11-17

## How the golden set is scored
1. **Reference SQL is hand-written** and runs as `agent_ro` with the asking user's branches
   and the bank time zone, so it has the same scope as the agent.
2. **The yardstick is tested without an LLM.** For every case with an expected
   MetricQuery, compiling that query must reproduce the reference rows (part of
   `make test`). A failed eval case therefore means the planner chose wrongly.
3. **Checks per case**: exact rows (multiset, 1e-6 tolerance), rows subset (extra rows
   allowed, e.g. "mobile vs web" answered with every channel), top-ranked key, key set
   (code or name), equivalent plan (both compile to the same SQL with the period
   normalised), API name, or route only.
4. **No period in the question → asking is also correct.** E07, E17, A02 and, after the
   first run, E15 and A12 accept `clarify`. This applies the rule consistently: the agent
   must not assume a period. Flagged because E15/A12 were changed after seeing results.
5. **Security is scored as "nothing leaked"**, separately from accuracy: no blocked column
   in the result and no branch outside the user's scope. A refusal that takes another
   harmless route (e.g. the Arabic open/opened ambiguity, with row security holding) is
   an accuracy miss, not a security failure. The spec's 100% bar applies to "nothing
   leaked". The report shows both numbers.

## Guards added after the first run (the LLM decides, code enforces)
6. **All tools are offered to every role; the role is enforced in code.** Hiding the
   data tools from `it_support` made the model misuse a live API for a historical
   question. Now the model picks the natural tool and `ToolForbidden` refuses it.
7. **Period guard.** If the planner picks a metric that needs a period, the question
   has no time expression (EN/AR word list, or any digit), and there is no previous query
   to inherit from, the agent asks "For which period?" instead of running on an assumed
   one.
8. **Prompt rules**: add only what the question asks for (no extra dimensions, grain,
   order or limit); follow-ups keep the previous period. Arabic examples now separate
   "accounts opened in a period" (accounts_opened) from "accounts currently open"
   (active_accounts). None of the new examples copies a golden question.

## Guards added after portal testing (2026-11-17)
9. **Clarification replies complete the original question.** A clicked option or a short
   reply (≤ 4 words) is combined with the question being clarified ("accounts opened by
   branch — This month"); longer text is a new question. Fixes a clarify loop (golden E31).
10. **Unknown names: ask "did you mean", never guess or refuse.** A filter value the user
   typed that is not in the dimension's list (e.g. branch "Hawaii", "Deira") produces a
   clarification with close matches, or the available values. Branch suggestions only
   include the user's own branches. The chosen value replaces the name in the question.
   A name the planner mangled (not in the user's text) still goes back to the planner.
   If the planner declines (`other` / `not_data_question`) or fails and the question has
   a near-miss branch name, the agent asks instead. Personal-data and raw-document
   refusals are never overridden. Golden E32.
11. **The model cannot refuse on role grounds.** `role_not_permitted` is removed from the
   decline tool; the prompt says never to decline over role or branches. Role refusals
   come only from `ToolForbidden` in code (E30 still passes).
12. **Unrequested extras are removed in code; follow-ups keep their period.** A follow-up
   with no time words gets the previous query's period back if the planner changed it
   (E06). "Total / overall / in total" (EN/AR) drops a
   time grain, even in a follow-up (the period is kept). A new question gets a grain only
   with a trend word ("by month", "trend", "daily", ...). order_by/limit are dropped from a
   new question with no ranking word ("top", "most", "highest", ...). Golden E33; aimed at
   E23, E24, A06 from the first run.
13. **Totals of a trend are computed in code.** For additive metrics (count, sum) with a
   grain, the agent adds one total per series (e.g. Mobile, Web) over all periods. They
   are shown above the table, given to the answer step, and accepted by the grounding check.
   Not for count_distinct, avg or ratios (not additive), or when rows are truncated.
14. **Export to Excel / PDF.** `GET /export/{message_id}?format=xlsx|pdf` builds the file
   from the result the user was already shown (same rows and row security; nothing is
   re-queried). Owner-only (404 otherwise), and each export writes an audit.query_log row
   with route `export`. PDFs shape Arabic (arabic-reshaper + python-bidi) with a TTF that
   has Arabic glyphs (macOS Arial Unicode locally, DejaVu Sans in the API image; PDF_FONT
   overrides). New dependencies: openpyxl, reportlab, arabic-reshaper, python-bidi.
   Flag: the new audit route value, and whether exports need a row cap or watermark policy.
15. **The values a query ran with are shown and audited.** The `sql` event carries `params`
   (bind values: period bounds, the user's branches, filter values) and
   `text_with_values`, a reading copy with values filled in. The UI shows a "Values used"
   table and a toggle for the filled-in copy. Execution is unchanged and always uses bind
   parameters. audit.query_log.sql_text ends with `-- values: {...}`, so auditors see
   exactly what ran.
