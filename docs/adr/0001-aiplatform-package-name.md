# ADR-0001: The shared platform package is `aiplatform/`, not `platform/`

Status: accepted (flagged for review) · 2026-11-17

## Context
DESIGN.md lays out the shared platform as a top-level `platform/` folder. In Python, a
top-level package named `platform` shadows the standard-library `platform` module.
Uvicorn, httpx, the vendor SDKs and pytest all `import platform`; with the repo root on
`sys.path` they would import our package instead and fail.

## Decision
The folder is `aiplatform/` with the same sub-packages (`gateway`, `llm`, `tools`,
`guardrails`, `audit`, `eval`). Everything else follows DESIGN.md unchanged.

## Consequences
Read every `platform/...` path in DESIGN.md and the specs as `aiplatform/...`.
CLAUDE.md uses the new name ("Provider SDKs are imported only inside aiplatform/llm").
