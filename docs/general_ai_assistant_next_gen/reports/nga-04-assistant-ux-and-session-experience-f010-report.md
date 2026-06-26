# NGA-04 / NGA-F010 Actor Report

Status: passed

## Result

`NGA-F010` is implemented with an environment caveat on the full e2e command.
The assistant UI now exposes plan/review/execute flow, live Activity state,
approval waits, context budget/compaction state, generated artifacts, and
artifact recovery on both desktop and mobile.

## Scope

Changed implementation paths:

- `web/src/pages/assistant/components/buildTimeline.ts`
- `web/src/pages/assistant/hooks/useChatSession.ts`
- `web/src/pages/assistant/index.tsx`
- `web/e2e/chat-experience.spec.ts`

Harness/report artifacts:

- `docs/general_ai_assistant_next_gen/reports/nga-04-assistant-ux-and-session-experience-plan.md`
- `docs/general_ai_assistant_next_gen/reports/nga-04-f010-rendered-desktop.png`
- `docs/general_ai_assistant_next_gen/reports/nga-04-f010-rendered-mobile.png`

No backend event contract, schema, dependency, migration, deployment, provider
credential, or production data path was changed.

## Implementation Notes

- Extended `buildTimeline()` so the existing Activity drawer derives rows from
  `processSummary.steps`, `processSummary.tools`, context budget/compaction
  telemetry, retrieved contexts, and generated artifacts.
- Mirrored legacy `task_planning` and `working_memory_update` events into the
  active assistant message `processSummary` while preserving the existing
  `workingMemory` state updates.
- Made approval events create/update process-summary tool rows even when the
  approval event is the first signal for a tool.
- Broadened the top-bar Activity target selection to include process summaries,
  contexts, and generated artifacts.
- Added a mobile Activity bottom sheet that reuses the existing `ActivityPanel`
  component instead of adding a second renderer.
- Added Playwright coverage for a mocked stream containing run, task planning,
  working memory, approval, context budget, compaction, artifact, and final text
  events.

## Red Evidence

Focused F010 test failed before implementation after selector cleanup:

```text
E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 E2E_USER_EMAIL=assistant-agent-state@example.com E2E_USER_PASSWORD=mock-password pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts -g "assistant activity surfaces agent run state approvals context and artifacts"
```

Failure:

```text
Error: expect(locator).toBeVisible() failed
Locator: getByText('Review requested change')
Timeout: 15000ms
```

The first attempt to run the focused test through the repo e2e stack did not
reach assertions because local dev dependencies required unavailable secrets:

```text
POSTGRES_PASSWORD is required
REDIS_PASSWORD is required
```

For the red/green UI proof, a lightweight local API stub was used with
`E2E_REUSE_SERVER=1`.

## Green Evidence

Focused F010 Playwright test:

```text
E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 E2E_USER_EMAIL=assistant-agent-state@example.com E2E_USER_PASSWORD=mock-password pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts -g "assistant activity surfaces agent run state approvals context and artifacts"
```

Result:

```text
1 passed
```

Rendered UI checks:

```text
desktop 1440x900: missing=[]
mobile 390x844: missing=[]
```

Screenshots:

- `docs/general_ai_assistant_next_gen/reports/nga-04-f010-rendered-desktop.png`
- `docs/general_ai_assistant_next_gen/reports/nga-04-f010-rendered-mobile.png`

Frontend checks:

```text
pnpm -C web type-check
```

Passed.

```text
pnpm -C web lint
```

Passed with 0 errors and 39 existing warnings.

```text
pnpm -C web build
```

Passed. Vite emitted the existing large-chunk warning.

API checks:

```text
uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/api/test_conversation_share_quiz.py
```

Result:

```text
11 passed, 1 Starlette deprecation warning
```

## Validation Evidence

The focused red/green Playwright evidence, frontend type-check/lint/build,
required API pytest, rendered desktop/mobile screenshots, and harness/static
checks are recorded in the Red Evidence, Green Evidence, Full E2E Command
Evidence, and Minimal-Change Notes sections of this actor report.

Harness/static checks after writeback:

```text
python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json >/dev/null
python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json >/dev/null
python3 -m json.tool docs/general_ai_assistant_next_gen/loop-contract.json >/dev/null
git diff --check
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score
```

Result:

```text
JSON checks passed
git diff --check passed
Harness validation passed
Quality score: 100 (excellent)
```

## Full E2E Command Evidence

The exact phase e2e command was run after implementation:

```text
E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 E2E_USER_EMAIL=assistant-agent-state@example.com E2E_USER_PASSWORD=mock-password pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts web/e2e/assistant-history.spec.ts web/e2e/assistant-memory.spec.ts
```

Result in this local environment:

```text
7 passed, 1 skipped, 5 failed
```

The new F010 test passed in this run. The failures were caused by the reused
lightweight API stub rather than the real backend:

- `assistant-history.spec.ts` failed creating seeded sessions.
- `assistant-memory.spec.ts` failed because `/api/v1/assistant/chat/stream`
  memory behavior was not implemented by the stub.
- `assistant stream path keeps a11y and performance budget` failed because the
  stubbed unmocked model/config path left the composer disabled.
- Two playground tests failed because the lightweight stub did not provide the
  real playground service/tool surfaces.

The real repo e2e stack could not be started in this environment without
`POSTGRES_PASSWORD` and `REDIS_PASSWORD`.

## Feature Oracle Updates

`NGA-F010` is marked passing in `feature-oracle.json` with actor report,
critic artifact, and rendered desktop/mobile screenshot evidence.

## Minimal Change

This was a narrow assistant UI slice. It reused existing Activity,
process-summary, right-panel, artifact, and mobile sheet surfaces and did not
change backend event, memory, RAG, session, or artifact contracts.

## Minimal-Change Notes

- Reused existing Activity, process-summary, right-panel, and artifact state.
- Did not introduce a new client-side agent-state store.
- Did not change public API payload shapes.
- Did not modify backend memory, RAG, event, session, or artifact contracts.
- Did not touch `NGA-F011`; it remains the next feature in `NGA-04`.
