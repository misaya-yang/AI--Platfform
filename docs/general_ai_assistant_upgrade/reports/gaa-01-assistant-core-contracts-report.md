# GAA-01 Assistant Core Contracts Report

**Phase:** GAA-01 Assistant Core Contracts

**Feature:** GAA-F002

**Status:** passed with pre-existing lint blocker recorded

**Date:** 2026-06-16

---

## Summary

Implemented one assistant-core contract slice: missing `assistant.artifacts` schema now maps to public artifact absence semantics. Session artifact lists continue returning an empty list; artifact metadata, download, and delete lookup paths now return `404 Artifact not found` instead of leaking a database schema failure as 500.

## Plan Followed

Plan file: `docs/general_ai_assistant_upgrade/reports/gaa-01-assistant-core-contracts-plan.md`.

## Files Changed

| File | Reason |
| --- | --- |
| `src/api/v1/assistant.py` | Added shared helper for schema-missing artifact lookup and applied it to metadata, download, and delete paths. |
| `tests/api/test_assistant_sessions.py` | Added parameterized 404 contract coverage for metadata, download, and delete lookup when `assistant.artifacts` is absent. |
| `.gitignore` | Added a targeted exception so `docs/general_ai_assistant_upgrade/**` is visible to git for handoff. |
| `docs/general_ai_assistant_upgrade/**` | Updated phase runtime files, oracle, reports, and handoff state. |

## Validation Evidence

| Gate | Command or Check | Result | Notes |
| --- | --- | --- | --- |
| Validation | `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py` | passed: 6 passed, 1 warning | Covers artifact schema-missing contract. |
| Regression | `uv run --extra dev --extra test pytest -q --no-cov tests/integration/test_assistant_core_isolation.py` | passed: 2 passed, 1 warning | Confirms assistant-service isolation invariant. |
| Regression | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_assistant_service.py tests/services/assistant/test_message_persistence.py tests/services/assistant/test_request_id_propagation.py` | passed: 29 passed, 1 warning | Covers required assistant-service target tests. |
| Validation | `uv run ruff check src/api/v1/assistant.py tests/api/test_assistant_sessions.py` | passed | Scoped to touched code and tests. |
| Regression | `uv run --extra dev --extra test pytest -q --no-cov` | passed: 2538 passed, 44 skipped, 82 warnings | Full local Python suite. |
| Validation | broad GAA-01 ruff command | blocked by pre-existing lint issues | `uv run ruff check src/api/v1/assistant.py apps/assistant-service/src/assistant_service packages/ai-gateway-core/src/ai_gateway_core tests/api/test_assistant_sessions.py tests/services/assistant` reports 822 existing ruff errors outside touched files. |

## Feature Oracle Updates

| Feature ID | Old Status | New Status | Evidence |
| --- | --- | --- | --- |
| GAA-F002 | failing | passing | This report plus pytest and scoped ruff evidence. |

## Progress Log Update

`progress-log.md` now records GAA-01 completion and points the next worker to GAA-02.

## Screenshots, Logs, or Eval Tables

No browser screenshot is required for this API/runtime phase. Command summaries above are the evidence.

## Blockers and Deviations

- Broad assistant-service ruff remains blocked by pre-existing lint issues outside this slice.
- `.gitignore` was edited outside GAA-01's original likely edit paths because the harness folder was ignored by repo policy; this is recorded in the plan and source packet.

## Handoff Notes

GAA-02 may proceed. UI work should assume artifact read paths now use public absence semantics for missing schema: empty list for session artifacts and 404 for single-artifact lookup/download/delete.
