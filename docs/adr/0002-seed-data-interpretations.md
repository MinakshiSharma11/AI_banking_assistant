# ADR-0002: Interpretations made while building the Phase 1 seed data

Status: accepted (flagged for review) · 2026-11-17

The data-agent spec leaves some seed details open or slightly inconsistent. These are
the choices made; each can be changed in `mocks/seed/` without touching other layers.

1. **Funnel vs digital share.** "Digital share of new accounts 20% → 55%" fixes approved
   applications at about 3,600 (one per digitally opened account). "70% submitted, 80% of
   those approved" needs 4,200. Both cannot hold, so we keep the share curve and the 80%
   approval rate; the submitted share comes out at about 64%.
2. **Digital channel = OBDX origination.** An account is "digital" when an APPROVED
   `obdx.origination_application` points at it; otherwise the compiler's default
   `'BRANCH'` applies (spec section 4). Corporate customers never originate digitally.
3. **CASA_ONBOARD is branch-assisted onboarding.** Completed cases link to branch-opened,
   authorised CASA accounts with the same customer and branch, and `ACCOUNT_CREATE` exits
   on the account's open date, so Atumverse and core counts reconcile by day. `wf_instance`
   has no `account_no` column, so the account number is carried in the payload
   (`account.accountNo`).
4. **LOAN_ORIG v1 payload shape** (the spec only says the amount was a flat `loanAmount`):
   all fields are flat: `employmentType, monthlySalary, product, loanAmount, tenorMonths,
   purpose, creditScore, dbr, decision, amountApproved, ratePct`. v2 is the nested shape
   from the spec. The oldest 30% of loans are v1.
5. **END-stage history rows** (`CLOSED`, `REJECTED`, `CANCELLED`) have `exited_at`,
   `action` and `sla_breached` NULL, so they never count as "exited visits" in dwell, SLA
   or return rates. START stages have no SLA and record `sla_breached = false`.
6. **RETURN loops** happen at `DOC_VERIFY`: the verifier sends the case back (action
   `RETURN`, `event_data.returnReason`) and it re-enters `DOC_VERIFY`. This makes the spec's
   golden query (`return_rate` filtered to stage `DOC_VERIFY`) answer "sent back at document
   verification".
7. **Row security for tables without a branch column:** digital users, sessions and
   applications use the customer's `home_branch`; transactions use the account's branch;
   Atumverse history, documents and flat rows inherit `wf_instance.branch_code`.
   Reference tables (branch_master, account_class, txn_type, wf_definition, wf_stage) are
   not filtered.
8. **agent_ro grants include `atum`** (the spec text says "core and obdx only"), because
   the Atumverse metrics need `wf_instance`, `wf_stage_history` and `wf_instance_flat`.
   Payload, document BLOB and OCR columns are excluded by column grants; `decode_error`
   is not granted.
9. **Weekday peaks follow the Kuwait work week** (Sunday–Thursday); Friday and Saturday
   are quiet.
10. **Seed anchor.** Data covers 730 days back from `SEED_DATE` (default: the current
    time in `BANK_TZ`). Same `SEED` + same anchor = identical data. The 50–150 "active
    right now" sessions are relative to the seed run, so they age out 15 minutes later.
11. **`it_ops` is entitled to all branches**: the role gate (live APIs only) restricts it,
    not its branch list.
12. **Audit table additions:** `assistant`, `conversation_id`, `message_id` columns
    (DESIGN.md audit store), and an append-only trigger on `audit.query_log`.
