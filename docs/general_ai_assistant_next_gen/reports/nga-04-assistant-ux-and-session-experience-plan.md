# NGA-04 / NGA-F010 Plan

## Scope
- Active phase: NGA-04 Assistant UX and Session Experience
- Active feature: NGA-F010
- Allowed implementation paths: `web/src/pages/assistant/**` and `web/e2e/chat-experience.spec.ts`
- Harness documentation paths: `docs/general_ai_assistant_next_gen/**`

## Observation
- The assistant stream handler already receives run lifecycle, step, tool, approval, context budget, context compaction, working-memory, and artifact events.
- The current Activity drawer is built from legacy `toolCalls`, `searchStatus`, and thinking content.
- Legacy `task_planning` and `working_memory_update` events update `workingMemory`, but the default V2 assistant UI does not render the task panel.
- The top-bar Activity chip ignores `processSummary`, generated artifacts, and context-only signals when choosing its latest target.

## Plan
1. Add a focused Playwright test in `web/e2e/chat-experience.spec.ts` that streams run, task, approval, context, and artifact events and expects Activity plus artifact surfaces to recover them.
2. Extend `buildTimeline` to derive Activity timeline rows from `processSummary.steps`, `processSummary.tools`, context budget/compaction, retrieved contexts, and generated artifacts.
3. Mirror `task_planning` and `working_memory_update` into the active message `processSummary` so plan/review/execute state is visible in V2.
4. Make approval events create or update process-summary tool rows even when the approval event is the first signal for that tool.
5. Broaden the page-level latest Activity target to include process summaries, contexts, and generated artifacts.

## Verification
- First run the focused Playwright test before implementation and preserve the failure as red evidence.
- After implementation, run the focused test, then the phase validation commands:
  - `pnpm -C web type-check`
  - `pnpm -C web lint`
  - `pnpm -C web build`
  - `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts web/e2e/assistant-history.spec.ts web/e2e/assistant-memory.spec.ts`
  - `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/api/test_conversation_share_quiz.py`
  - strict harness validator from `loop-contract.json`

## Constraints
- No backend contract changes.
- No dependency changes.
- No migrations, deployments, secrets, destructive git operations, or commits.
- Keep the implementation minimal and local to the active feature.

# NGA-04 / NGA-F011 Plan

## Scope
- Active phase: NGA-04 Assistant UX and Session Experience
- Active feature: NGA-F011
- Allowed implementation paths: `web/src/pages/assistant/**`, `web/e2e/chat-experience.spec.ts`, and harness documentation under `docs/general_ai_assistant_next_gen/**`
- Target behavior: restored assistant sessions expose artifact continuity and share state without double-counting the same persisted artifact.

## Observation
- `useChatSession()` already restores session history, fetches `getSessionArtifacts(sessionId)`, hydrates assistant messages from persisted `metadata.artifact_ids`, and rebuilds `codeExecution.outputFiles` for the latest artifact-producing assistant message.
- `index.tsx` renders the top-bar Artifacts chip from `artifacts.length + codeExecution.outputFiles.length`, while `ShareDialog` receives only `artifacts.length`.
- A restored session with one persisted artifact can therefore show two artifact affordance counts because the same artifact exists in both restored collections.

## Plan
1. Add a focused Playwright test in `web/e2e/chat-experience.spec.ts` that preloads a restored assistant session with one persisted artifact, verifies the restored answer/artifact affordance, opens Share, and expects the unique artifact count to be one.
2. Run that focused test before production changes and record the red failure.
3. Add a small assistant-page helper that counts unique artifact IDs across persisted artifacts and current-run output files.
4. Use that unique count for the Artifacts chip and Share dialog without changing artifact rendering, APIs, or backend ownership contracts.
5. Run the focused test green, then the phase validation commands and browser layout checks.

## Verification
- Focused red/green: `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts --grep "restores session artifacts"`
- Phase validation:
  - `pnpm -C web type-check`
  - `pnpm -C web lint`
  - `pnpm -C web build`
  - `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts web/e2e/assistant-history.spec.ts web/e2e/assistant-memory.spec.ts`
  - `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/api/test_conversation_share_quiz.py`
  - strict harness validator from `loop-contract.json`

## Constraints
- No backend contract changes.
- No dependency changes.
- No migrations, deployments, secrets, destructive git operations, or commits.
- Keep the implementation minimal and local to the active feature.
