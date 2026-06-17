# GAA-02 Assistant User Experience Report

**Phase:** GAA-02 Assistant User Experience

**Feature:** GAA-F003

**Status:** passed

**Date:** 2026-06-16

---

## Summary

Implemented and executed a bounded GAA-02 browser test slice for assistant route access: authenticated users with `conversation:playground:access` can reach `/assistant`, while users whose only role is `model_tester` are redirected to `/playground` and do not see the assistant sidebar entry.

The full page walkthrough also passed after fixing two runtime 500s exposed by the browser gate: disabled Confluence read endpoints now return empty lists, and API key reads/writes tolerate the baseline `api_keys.permissions` schema while preserving the public `scopes` response contract.

## Plan Followed

Plan file: `docs/general_ai_assistant_upgrade/reports/gaa-02-assistant-user-experience-plan.md`.

## Files Changed

| File | Reason |
| --- | --- |
| `web/e2e/support/helpers.ts` | Added `installClientAuth` for deterministic route tests that seed local auth and still validate through mocked `/api/v1/auth/me`. |
| `web/e2e/chat-experience.spec.ts` | Added route coverage for permitted assistant users and model-tester-only redirect behavior. |
| `src/api/v1/confluence.py` | Expanded runtime fix: disabled optional Confluence list endpoints now return `[]` instead of 500 during page walkthrough. |
| `tests/api/test_confluence_disabled.py` | Added direct coverage for disabled Confluence list endpoints. |
| `src/services/auth/api_key_service.py` | Expanded runtime fix: API key service maps legacy/base `permissions` to public `scopes` when the `scopes` column is absent. |
| `tests/services/test_api_key_service_legacy.py` | Added legacy schema coverage for API key list, detail, create, and validate flows. |
| `docs/general_ai_assistant_upgrade/**` | Recorded phase plan, report, oracle state, progress, and handoff evidence. |

## Validation Evidence

| Gate | Command or Check | Result | Notes |
| --- | --- | --- | --- |
| Validation | `pnpm -C web type-check` | passed | TypeScript accepted the helper and test changes. |
| Validation | `pnpm -C web build` | passed | Vite build completed; existing large chunk warnings remain. |
| Validation | `pnpm -C web lint` | passed with warnings | 0 errors, 39 warnings. Warnings match existing hook/dependency quality backlog. |
| Validation | `git diff --check` | passed | No whitespace errors in current diff. |
| Discovery | `E2E_BASE_URL=http://127.0.0.1:1 E2E_API_URL=http://127.0.0.1:2 pnpm -C web exec playwright test --list web/e2e/chat-experience.spec.ts -g "assistant route"` | passed | Listed both new tests: permitted-user route and model-tester redirect. |
| Environment | `pnpm -C web exec playwright install chromium` | passed | Installed the Playwright Chromium runtime needed for local browser checks. |
| Runtime | `docker restart ai-gateway-backend` plus health poll | passed | Backend container returned `healthy` after loading touched backend files. |
| API regression | `uv run --extra dev --extra test pytest -q --no-cov tests/services/test_api_key_service_legacy.py tests/api/test_confluence_disabled.py` | passed: 8 passed, 1 warning | Covers runtime fixes exposed by the page walkthrough. |
| Lint regression | `uv run ruff check src/services/auth/api_key_service.py src/api/v1/confluence.py tests/services/test_api_key_service_legacy.py tests/api/test_confluence_disabled.py` | passed | Touched backend files pass ruff after import sorting. |
| Browser | `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts -g "assistant route"` | passed: 2 passed | Verified permitted assistant access and model-tester redirect against the running stack. |
| Browser | `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/site-walkthrough.spec.ts` | passed: 1 passed | Walked main pages and rejected console errors, page errors, and HTTP responses >= 400. |
| Harness | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_upgrade --strict --quality-score` | passed: quality score 100 | Confirms updated harness files remain structurally valid. |

## Route and Browser Findings

| Route | Expected Behavior | Evidence |
| --- | --- | --- |
| `/assistant` | A permitted authenticated user reaches the assistant route and sees `#assistant-chat-composer` plus the assistant nav link. | Playwright test `assistant route admits permitted users` passed against `http://127.0.0.1:8081`. |
| `/assistant` as model-tester-only | User redirects to `/playground`, assistant nav link is absent, playground composer is visible. | Playwright test `assistant route keeps model testers playground-only` passed against `http://127.0.0.1:8081`. |
| Main page walkthrough | Sidebar routes load without obvious UI breakage, console errors, page errors, or HTTP responses >= 400. | `web/e2e/site-walkthrough.spec.ts` passed after the Confluence-disabled and API-key legacy schema fixes. |

## Blockers and Deviations

- No GAA-02 blocker remains.
- Scope was expanded from frontend-only E2E files into two backend read paths because the browser walkthrough exposed real runtime 500s. The fixes were limited to optional Confluence read behavior and API key schema compatibility.
- Broader deployment checks still require operator-provided `.env` and provider credentials; those belong to GAA-04.

## Code Review Notes

- `installClientAuth` is E2E-only and does not touch production auth code.
- The helper seeds local persisted auth but also mocks `/api/v1/auth/me`, so `useAuthSessionGuard` still exercises the current-user validation path.
- The new tests stay inside existing mocked E2E patterns and add no dependency.
- Disabled Confluence list endpoints now preserve a safe empty-state contract without enabling write/sync operations when the optional integration is off.
- API key compatibility preserves the public `scopes` field and chooses only between fixed column names, avoiding user-controlled SQL fragments.

## Feature Oracle Updates

| Feature ID | Old Status | New Status | Evidence |
| --- | --- | --- | --- |
| GAA-F003 | blocked | passing | Frontend static checks passed, assistant route E2E passed 2/2, site walkthrough passed 1/1, and backend runtime fixes have targeted pytest plus ruff evidence. |

## Handoff Notes

GAA-03 can start. Begin with deterministic assistant eval/safety coverage and keep external provider calls mockable.
