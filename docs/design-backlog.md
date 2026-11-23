# Design backlog: items to add to DESIGN.md

Decisions taken during the build that DESIGN.md does not yet cover. Each should be added
to DESIGN.md (most as rows in "Open decisions"), then removed from this list.

| # | Item | POC choice (who decided, when) | Open question for DESIGN.md | Needed before | Ref |
|---|---|---|---|---|---|
| 1 | How answers are delivered | Checked-then-shown: the answer is held until the grounding check passes, then shown whole (user, 2026-11-17) | Keep, or stream word by word and replace the text if the check fails? Trade-off: no unverified number is ever on screen vs. time to first token | Phase 3 (pilot) | ADR-0003 §3 |
| 2 | Conversation memory store | In-memory per API process: lost on restart, not shared across replicas (user, 2026-11-17) | Move to Postgres or Redis for restarts, horizontal scaling and retention aligned with record-keeping policy? | Phase 3 (pilot) | ADR-0004 |
| 3 | Refusal tool | A fifth planner tool, `decline`; refusal texts fixed in code (EN/AR) | Confirm the refusal wording with compliance; extend the reasons per assistant | Phase 3 (pilot) | ADR-0003 §1 |
| 4 | Local model sizing | qwen3:8b struggles with free-SQL comparisons on a 16 GB laptop | Minimum local or in-region model size for the SQL fallback, or restrict the fallback to hosted models | Phase 3 (pilot) | Phase 3 report |
