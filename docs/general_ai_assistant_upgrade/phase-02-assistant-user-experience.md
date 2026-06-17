# Phase 02 - Assistant User Experience

> For agentic workers: enter plan-first mode before editing. Execute this phase only, write evidence, and do not advance until acceptance gates pass or blockers are documented.

**Goal:** Upgrade one assistant UI slice with route and browser evidence.

**Architecture:** The React/Vite frontend uses `web/src/router.tsx`, protected routes, auth store, API/SSE helpers, runtime-injected config, and assistant page components under `web/src/pages/assistant`.

**Tech Stack:** React 19, Vite 8, TypeScript, pnpm, Playwright, TanStack Query, Zustand, assistant API and SSE helpers.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "hybrid",
  "phase": {
    "id": "GAA-02",
    "number": "02",
    "title": "Assistant User Experience",
    "status": "ready",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_upgrade",
    "phase_file": "docs/general_ai_assistant_upgrade/phase-02-assistant-user-experience.md",
    "depends_on": ["GAA-01"],
    "unlocks": ["GAA-03"]
  },
  "goal": {
    "target": "Upgrade one assistant UI slice with route and browser evidence.",
    "prompt": "Complete GAA-02 Assistant User Experience for `.` by following `docs/general_ai_assistant_upgrade/phase-02-assistant-user-experience.md`; work on feature-oracle item GAA-F003; preserve auth, runtime config, SSE, telemetry, route, and accessibility contracts; finish only after validation, regression, browser, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_upgrade/reports/gaa-02-assistant-user-experience-plan.md",
    "completion_report": "docs/general_ai_assistant_upgrade/reports/gaa-02-assistant-user-experience-report.md"
  },
  "runtime": {
    "feature_oracle": "docs/general_ai_assistant_upgrade/feature-oracle.json",
    "loop_contract": "docs/general_ai_assistant_upgrade/loop-contract.json",
    "loop_state": "docs/general_ai_assistant_upgrade/loop-state.json",
    "progress_log": "docs/general_ai_assistant_upgrade/progress-log.md",
    "handoff": "docs/general_ai_assistant_upgrade/agent-handoff.md",
    "continuity_ledger": "docs/general_ai_assistant_upgrade/continuity-ledger.md",
    "next_window_prompt": "docs/general_ai_assistant_upgrade/next-window-prompt.md",
    "session_boot": {"read_progress": true, "run_baseline_check": true, "update_progress_before_exit": true},
    "agent_roles": ["planner", "generator", "evaluator"]
  },
  "context": {
    "read_first": ["docs/general_ai_assistant_upgrade/README.md", "docs/general_ai_assistant_upgrade/phase-manifest.md", "docs/general_ai_assistant_upgrade/loop-contract.json", "docs/general_ai_assistant_upgrade/loop-state.json", "docs/general_ai_assistant_upgrade/feature-oracle.json", "docs/general_ai_assistant_upgrade/progress-log.md", "docs/general_ai_assistant_upgrade/agent-handoff.md", "docs/general_ai_assistant_upgrade/continuity-ledger.md", "docs/general_ai_assistant_upgrade/next-window-prompt.md", "docs/general_ai_assistant_upgrade/source-packet.md", "docs/general_ai_assistant_upgrade/reports/gaa-01-assistant-core-contracts-report.md", "docs/general_ai_assistant_upgrade/phase-02-assistant-user-experience.md"],
    "primary_context": ["web/src/router.tsx", "web/src/pages/assistant/index.tsx", "web/src/pages/assistant/components", "web/src/pages/assistant/hooks", "web/src/lib/api.ts", "web/src/lib/sse.ts", "web/src/config/runtime.ts", "web/src/store/useAuthStore.ts", "web/e2e/site-walkthrough.spec.ts", "web/e2e/assistant-history.spec.ts", "web/e2e/chat-experience.spec.ts"],
    "context_budget": "focused",
    "do_not_load_unless": ["backend service internals outside API contract", "provider dashboards", "secret files", "production logs"]
  },
  "boundaries": {
    "likely_edit_paths": ["web/src/pages/assistant/**", "web/src/features/chat/**", "web/src/lib/api.ts", "web/src/lib/sse.ts", "web/src/config/runtime.ts", "web/src/router.tsx", "web/e2e/**", "docs/general_ai_assistant_upgrade/**"],
    "do_not_edit": [".env", "src/api/v1/**", "apps/assistant-service/**", "database/migrations/**", "production systems", "provider dashboards"],
    "external_inputs": ["local compose stack", "E2E user credentials", "browser screenshots"],
    "secrets_required": ["E2E user password in local test setup only"]
  },
  "tool_policy": {
    "allowed_tools": ["repo search", "shell validation", "apply_patch", "browser automation", "code review", "code simplifier"],
    "approval_required": ["new dependency", "deployment", "production data mutation", "external provider change"],
    "dangerous_commands": ["git reset --hard", "rm -rf", "docker compose down -v", "DROP SCHEMA", "TRUNCATE"]
  },
  "risk": {
    "tags": ["frontend", "ui", "browser", "auth"],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": true,
    "ai_eval_required": false,
    "external_service_required": false,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {"id": "frontend-typecheck", "cwd": ".", "command": "pnpm -C web type-check", "expected": "command exits 0", "required": true},
      {"id": "frontend-build", "cwd": ".", "command": "pnpm -C web build", "expected": "command exits 0", "required": true},
      {"id": "frontend-lint", "cwd": ".", "command": "pnpm -C web lint", "expected": "command exits 0 and warnings are recorded", "required": true},
      {"id": "assistant-browser-walkthrough", "cwd": ".", "command": "pnpm -C web e2e -- web/e2e/site-walkthrough.spec.ts", "expected": "command exits 0 with screenshots for visible authenticated routes", "required": true}
    ],
    "browser_checks": ["Login as E2E user and visit /assistant at desktop 1440x900", "Visit /playground and confirm model_tester redirect behavior remains valid", "Run site walkthrough and attach screenshots for visible sidebar routes", "Check console errors, page errors, and HTTP responses >= 400 are empty or documented"],
    "regression_scope": ["login flow", "protected route permissions", "runtime config precedence", "assistant chat input", "SSE streaming parser", "telemetry sendBeacon guard", "responsive assistant layout"],
    "compliance_gates": ["no auth bypass", "no secret values in browser logs", "keyboard focus visible for primary controls", "error states do not expose provider secrets", "role-based assistant access remains enforced"],
    "acceptance_gates": ["GAA-F003 status has screenshot or Playwright evidence", "type-check and build pass", "lint output is recorded", "assistant route works for authenticated user", "continuity ledger records UI contract changes", "review evidence is recorded", "minimal-change scope is documented"],
    "rollback_plan": ["revert frontend files touched in this phase", "revert E2E files touched in this phase", "restore previous runtime config behavior if browser smoke fails"]
  },
  "evidence": {
    "outputs": ["docs/general_ai_assistant_upgrade/reports/gaa-02-assistant-user-experience-report.md", "web/test-results or Playwright screenshot paths"],
    "required_artifacts": ["phase report", "frontend command output summary", "browser screenshot paths", "progress-log entry", "feature-oracle evidence", "continuity-ledger update", "source-packet update", "handoff update"],
    "waiver_policy": "A skipped browser gate requires a named missing runtime input and a residual risk note.",
    "next_phase_handoff": "Unlock GAA-03 only after UI behavior and route contracts are recorded."
  },
  "stop_conditions": ["GAA-01 report is missing", "E2E user is unavailable", "compose stack is unavailable and no browser waiver exists", "auth route behavior cannot be verified", "edits outside frontend boundary are required"]
}
```

## Coding Agent Contract

- PHASE_ID: GAA-02
- GOAL_TARGET: Upgrade one assistant UI slice with route and browser evidence.
- GOAL_PROMPT: Complete GAA-02 Assistant User Experience for `.` by following `docs/general_ai_assistant_upgrade/phase-02-assistant-user-experience.md`; work on feature-oracle item GAA-F003; preserve auth, runtime config, SSE, telemetry, route, and accessibility contracts; finish only after validation, regression, browser, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: GAA-01
- READ_FIRST: `docs/general_ai_assistant_upgrade/README.md`, `docs/general_ai_assistant_upgrade/phase-manifest.md`, `reports/gaa-01-assistant-core-contracts-report.md`, this file
- PRIMARY_CONTEXT: `web/src/router.tsx`, `web/src/pages/assistant/index.tsx`, `web/src/pages/assistant/components`, `web/src/pages/assistant/hooks`, `web/src/lib/api.ts`, `web/src/lib/sse.ts`, `web/src/config/runtime.ts`, `web/src/store/useAuthStore.ts`, `web/e2e/site-walkthrough.spec.ts`, `web/e2e/assistant-history.spec.ts`, `web/e2e/chat-experience.spec.ts`
- LIKELY_EDIT_PATHS: `web/src/pages/assistant/**`, `web/src/features/chat/**`, `web/src/lib/api.ts`, `web/src/lib/sse.ts`, `web/src/config/runtime.ts`, `web/src/router.tsx`, `web/e2e/**`, `docs/general_ai_assistant_upgrade/**`
- DO_NOT_EDIT: `.env`, `src/api/v1/**`, `apps/assistant-service/**`, `database/migrations/**`, production systems, provider dashboards
- EXECUTION_MODE: plan-first; implement stepwise; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `pnpm -C web type-check`; `pnpm -C web build`; `pnpm -C web lint`; `pnpm -C web e2e -- web/e2e/site-walkthrough.spec.ts`
- BROWSER_CHECKS: `/assistant` desktop 1440x900; `/playground` redirect/access regression; site walkthrough screenshots; console/page/network error checks
- REGRESSION_SCOPE: login, protected routes, runtime config, assistant input, SSE parser, telemetry, responsive layout
- COMPLIANCE_GATES: no auth bypass; no secret logs; focus visible; provider errors redacted; role access enforced
- ROLLBACK_PLAN: revert frontend and E2E files touched in this phase; restore prior runtime config behavior if smoke fails
- ACCEPTANCE_GATES: GAA-F003 has screenshot or Playwright evidence; type/build pass; lint recorded; assistant route works; ledger records UI changes; review evidence recorded; minimal-change scope documented
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_upgrade/reports/gaa-02-assistant-user-experience-report.md`
- STOP_CONDITIONS: GAA-01 missing, E2E user unavailable, compose stack unavailable without waiver, auth behavior not verifiable, backend edits required

## Task Spec

Select one assistant UI capability, implement it, and prove it through compile checks plus browser evidence.

## Problem Boundary

This phase is frontend-only except for reading API contracts from previous reports. Backend behavior changes belong to GAA-01.

## Context Policy

Load listed frontend files first. Add backend context only when the GAA-01 report documents a contract that the UI consumes.

## Requirements

### R1 Route Behavior

The `/assistant` route must work for an authenticated permitted user and preserve role redirect behavior.

### R2 Runtime Config

Frontend code must respect runtime config precedence for API base URL, auth domain, support email, telemetry endpoint, and SSE debug.

### R3 Browser Evidence

Page checks must include screenshots or Playwright artifacts.

## Test and Regression Requirements

Run the frontend commands and record lint warnings rather than hiding them.

## Compliance and Safety Requirements

Do not expose provider keys, tokens, user files, or raw backend stack traces in UI logs.

## Rollback and Recovery

Revert frontend files touched in this phase if browser or build checks fail.

## Execution Capture

Write `reports/gaa-02-assistant-user-experience-report.md` with route list, screenshot paths, command summaries, and skipped gates.

## Evaluator Protocol

Inspect screenshots, browser console output, route permissions, and network failures before accepting completion.

## Acceptance Criteria

- Required frontend commands pass.
- Browser checks pass or have documented blocker evidence.
- GAA-F003 has evidence.
- GAA-03 receives UI behavior facts for eval design.

## Risks

- Static build can pass while authenticated routes fail; Playwright evidence is required for phase completion.
