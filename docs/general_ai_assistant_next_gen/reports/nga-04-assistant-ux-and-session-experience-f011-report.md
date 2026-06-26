# NGA-04 / NGA-F011 Actor Report

Status: passed

## Status

Passing.

## Scope

Feature `NGA-F011` covers durable assistant session recovery, share state, and artifact continuity. This slice stayed inside:

- `web/src/pages/assistant/index.tsx`
- `web/e2e/chat-experience.spec.ts`
- `docs/general_ai_assistant_next_gen/**`

No backend API shape, database schema, migration, dependency, deployment, provider credential, production data, or secret-handling path was changed.

## Implementation

- Extended the mocked assistant Playwright harness so a test can preload assistant sessions, restored history, persisted artifacts, and share responses.
- Added a focused browser test: `assistant restores session artifacts into unique artifact and share counts`.
- Added `countUniqueArtifactAffordances()` in the assistant page to count unique persisted artifact IDs across restored `artifacts` and reconstructed `codeExecution.outputFiles`.
- Reused the unique count for the desktop Artifacts chip and Share dialog artifact count.

## Red / Green Evidence

- Red: `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts --grep "restores session artifacts"`
  - Failed before implementation because the restored session rendered `Artifacts 2` for one persisted artifact.
- Green: same command passed after implementation.
  - Result: `1 passed`.

## Validation Evidence

| Command / Check | Result |
| --- | --- |
| `pnpm -C web type-check` | Passed. |
| `pnpm -C web lint` | Passed with 0 errors and 39 existing warnings. |
| `pnpm -C web build` | Passed with existing Vite large-chunk warning. |
| Required assistant browser command with reused lightweight stack | 8 passed, 1 skipped, 5 failed due missing real backend/playground behavior in the local stub. F010 and F011 mocked assistant tests passed. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/api/test_conversation_share_quiz.py` | Passed: 11 passed, 1 Starlette deprecation warning. |
| In-app Browser plugin | Unavailable: `Browser is not available: iab`; Playwright fallback used. |
| Desktop rendered check `1440x900` | Passed with no horizontal overflow: `scrollWidth=1440`, `clientWidth=1440`; screenshot `reports/nga-04-f011-rendered-desktop.png`. |
| Mobile rendered check `390x844` | Passed with no horizontal overflow: `scrollWidth=390`, `clientWidth=390`; screenshot `reports/nga-04-f011-rendered-mobile.png`. |

The real E2E stack could not be started by the Playwright webServer because local `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are not provided. To avoid inventing or printing secrets, focused browser evidence reused a local Vite server and a lightweight API stub.

## Feature Oracle Updates

`NGA-F011` is marked passing in `feature-oracle.json` with actor report,
critic artifact, and rendered desktop/mobile screenshot evidence.

## Minimal Change

This was a narrow assistant session UX slice. It reused existing restored
session history, artifact hydration, current-run output-file reconstruction,
and share API client behavior; only the artifact affordance count and focused
browser fixture coverage changed.

## Minimal-Change Notes

- The fix changes only artifact affordance counting; restored artifact rendering, share API calls, session APIs, and ownership checks remain on existing contracts.
- The UI now counts the same artifact ID once even when the session restore path has both a persisted artifact object and a reconstructed current-run output file.
- Test fixture additions are local to `chat-experience.spec.ts` and preserve existing default behavior when no preloaded session data is supplied.

## Acceptance Notes

- Durable resume: restored history loads through `getSessionHistory()`.
- Artifact continuity: persisted artifact IDs hydrate the inline artifact card and desktop Artifacts chip.
- Share continuity: Share dialog exposes one unique artifact and sends `include_artifacts: true`.
- Mobile: restored message, artifact card, input, model control, activity link, and history access remain reachable without horizontal overflow.
