# Phase 03 - Real API, SSE, chat, tool approval, cancellation, resume, history, UI, Knowledge, and Qwen journeys pass.

- PHASE_ID: ARU-03
- FEATURE_ID: ARU-F004
- DEPENDS_ON: ARU-02

## Outcome

The compiled candidate completes every real API, SSE, browser, Knowledge, negative, and DashScope/Qwen journey in the PRD matrix.

## Scope

In:

- Existing backend/runtime regression commands, live HTTP/SSE probes, and `web/e2e/` acceptance.
- The ignored persistent E2E identity and runtime provider configuration, read only at execution time.

Out:

- New test frameworks, replacement accounts, provider changes, RAG algorithm changes, or unrelated UI work.

## Done when

- [ ] Backend API/SSE, tool approval allow/deny/failure/idempotency, cancellation/resume/history, negative tenant/auth, Knowledge, UI clicks, and Qwen streaming/tool journeys pass against the candidate.
- [ ] Browser console/network state has no blocking failure.
- [ ] Existing affected behavior still passes its smallest relevant regression check.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Runtime/API gates | Existing `agent-runtime-*` and Agent V2 contract commands in `docs/harness/commands.md` | Real candidate HTTP/SSE and platform contracts behave correctly. |
| UI/Qwen | Existing Playwright acceptance plus one live Qwen streaming and one tool journey | Compiled UI and configured provider work end to end. |

Use the smallest check that can falsify the outcome. Add runtime, browser, migration, or reviewer evidence only when the outcome requires it.

## Stop or confirm

- Stop when a dependency is incomplete, the same failure repeats without a new hypothesis, or the iteration cap is reached.
- Set `waiting_confirmation` only for a real authority boundary.
- Never print credentials or provider keys; stop if the existing runtime configuration is absent rather than inventing secrets.
