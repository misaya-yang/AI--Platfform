# Phase 04 - Assistant UX and Session Experience

> For agentic workers: execute this phase only after NGA-03 passes or is explicitly waived in its report. Work on NGA-F010 and NGA-F011.

**Goal:** Make the assistant UI expose the agent harness state, capability choices, memory/context state, approvals, artifacts, and durable session controls in a usable workflow.

**Architecture:** NGA-04 consumes the backend contracts from NGA-01 through NGA-03 and maps them into `web/src/pages/assistant`, SSE event parsing, activity/timeline panels, connector and customization surfaces, conversation sidebar, share dialog, artifact panels, and focused assistant Playwright coverage.

**Tech Stack:** React, TypeScript, Vite, assistant page components/hooks, SSE event model, Playwright, pnpm typecheck/lint/build, and harness validator.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "execution",
  "phase": {
    "id": "NGA-04",
    "number": "04",
    "title": "Assistant UX and Session Experience",
    "status": "planned",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_next_gen",
    "phase_file": "docs/general_ai_assistant_next_gen/phase-04-assistant-ux-and-session-experience.md",
    "depends_on": [
      "NGA-03"
    ],
    "unlocks": [
      "NGA-05"
    ]
  },
  "goal": {
    "target": "Expose agent state, capabilities, memory/context state, approvals, artifacts, and durable sessions in the assistant UI.",
    "prompt": "Complete NGA-04 Assistant UX and Session Experience for `.` by following `docs/general_ai_assistant_next_gen/phase-04-assistant-ux-and-session-experience.md`; work on NGA-F010 and NGA-F011; stay inside the named web, assistant API contract, focused test, and harness boundaries; finish only after validation, regression, browser, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_next_gen/reports/nga-04-assistant-ux-and-session-experience-plan.md",
    "completion_report": "docs/general_ai_assistant_next_gen/reports/nga-04-assistant-ux-and-session-experience-report.md"
  },
  "runtime": {
    "feature_oracle": "docs/general_ai_assistant_next_gen/feature-oracle.json",
    "loop_contract": "docs/general_ai_assistant_next_gen/loop-contract.json",
    "loop_state": "docs/general_ai_assistant_next_gen/loop-state.json",
    "progress_log": "docs/general_ai_assistant_next_gen/progress-log.md",
    "handoff": "docs/general_ai_assistant_next_gen/agent-handoff.md",
    "continuity_ledger": "docs/general_ai_assistant_next_gen/continuity-ledger.md",
    "next_window_prompt": "docs/general_ai_assistant_next_gen/next-window-prompt.md",
    "session_boot": {
      "read_progress": true,
      "run_baseline_check": true,
      "update_progress_before_exit": true
    },
    "agent_roles": [
      "planner",
      "generator",
      "critic"
    ],
    "context_profile": "docs/general_ai_assistant_next_gen/context-profile.json"
  },
  "context": {
    "read_first": [
      "docs/general_ai_assistant_next_gen/context-profile.json",
      "docs/general_ai_assistant_next_gen/loop-state.json",
      "docs/general_ai_assistant_next_gen/phase-04-assistant-ux-and-session-experience.md"
    ],
    "primary_context": [
      "web/src/pages/assistant/index.tsx",
      "web/src/pages/assistant/sse-events.ts",
      "web/src/pages/assistant/types.ts",
      "web/src/pages/assistant/hooks/useChatSession.ts"
    ],
    "context_budget": "focused",
    "do_not_load_unless": [
      "backend run-loop internals",
      "database migrations",
      "provider credentials",
      "production browser sessions",
      "docs/general_ai_assistant_next_gen/README.md only when broad harness orientation is missing",
      "docs/general_ai_assistant_next_gen/phase-manifest.md only when phase index or validation matrix is needed",
      "docs/general_ai_assistant_next_gen/source-packet.md only when code facts, prior evidence, or source assumptions are needed",
      "docs/general_ai_assistant_next_gen/loop-contract.json only when loop semantics are unclear",
      "docs/general_ai_assistant_next_gen/feature-oracle.json only when selecting or updating the active feature status/evidence/notes",
      "docs/general_ai_assistant_next_gen/progress-log.md only when recording session progress, validation, or blockers",
      "docs/general_ai_assistant_next_gen/agent-handoff.md only when preparing or consuming planner/generator/critic handoff",
      "docs/general_ai_assistant_next_gen/continuity-ledger.md only when checking downstream contracts or writing code-summary handoff",
      "docs/general_ai_assistant_next_gen/next-window-prompt.md only when preparing a fresh continuation prompt"
    ]
  },
  "boundaries": {
    "likely_edit_paths": [
      "web/src/pages/assistant/**",
      "web/src/components/ConversationSidebar.tsx",
      "web/src/components/agent/**",
      "web/src/components/artifacts/**",
      "web/e2e/chat-experience.spec.ts",
      "web/e2e/assistant-history.spec.ts",
      "web/e2e/assistant-memory.spec.ts",
      "tests/api/test_assistant_sessions.py",
      "tests/api/test_conversation_share_quiz.py",
      "docs/general_ai_assistant_next_gen/**"
    ],
    "do_not_edit": [
      "backend harness contract from NGA-01 without dependency writeback",
      "memory/RAG contract from NGA-03 without dependency writeback",
      "database migrations without approval",
      "env files",
      "production data"
    ],
    "external_inputs": [
      "Use local or mocked API responses for Playwright when live model providers are unavailable."
    ],
    "secrets_required": []
  },
  "tool_policy": {
    "allowed_tools": [
      "rg",
      "sed",
      "apply_patch",
      "pnpm type-check",
      "pnpm lint",
      "pnpm build",
      "Playwright",
      "uv pytest",
      "harness validator"
    ],
    "approval_required": [
      "live provider credential use",
      "production data access",
      "deployment",
      "schema migration",
      "destructive git operations"
    ],
    "dangerous_commands": [
      "git reset --hard",
      "git push --force",
      "docker compose down -v",
      "database DROP or TRUNCATE"
    ]
  },
  "risk": {
    "tags": [
      "ui",
      "frontend",
      "browser",
      "auth",
      "security"
    ],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": true,
    "ai_eval_required": false,
    "external_service_required": false,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {
        "id": "frontend-typecheck",
        "cwd": ".",
        "command": "pnpm -C web type-check",
        "expected": "Assistant frontend TypeScript changes type-check successfully.",
        "required": true
      },
      {
        "id": "frontend-lint",
        "cwd": ".",
        "command": "pnpm -C web lint",
        "expected": "Frontend lint exits successfully; any existing warnings are reported.",
        "required": true
      },
      {
        "id": "frontend-build",
        "cwd": ".",
        "command": "pnpm -C web build",
        "expected": "Frontend production build succeeds.",
        "required": true
      },
      {
        "id": "assistant-browser-smoke",
        "cwd": ".",
        "command": "E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts web/e2e/assistant-history.spec.ts web/e2e/assistant-memory.spec.ts",
        "expected": "Assistant chat, history, and memory browser checks pass or record a precise missing-runtime blocker.",
        "required": true
      },
      {
        "id": "session-api-regression",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/api/test_conversation_share_quiz.py",
        "expected": "Assistant session and share API contract tests pass.",
        "required": true
      },
      {
        "id": "strict-harness-validation",
        "cwd": ".",
        "command": "python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score",
        "expected": "Harness remains strict-validator clean after NGA-04 evidence writeback.",
        "required": true
      }
    ],
    "browser_checks": [
      "Route `/assistant` at desktop viewport 1440x900 shows the conversation area, activity/timeline surface, capability controls, and artifact/session affordances without overlap.",
      "Route `/assistant` at mobile viewport 390x844 has no horizontal overflow and keeps the input, history access, and primary activity state reachable.",
      "SSE or mocked stream events render run lifecycle, tool lifecycle, approval, context, memory, and artifact states.",
      "Session resume, share, and memory-state surfaces either work or show a precise blocked state."
    ],
    "regression_scope": [
      "Existing login-protected assistant route remains accessible after authentication.",
      "Existing chat input, file upload, model selector, KB selector, web search toggle, generated document/image/quiz views, and prompt suggestions remain usable.",
      "Conversation sidebar continues to load, select, and create sessions.",
      "Independent critic evidence confirms the UI change is a minimal-change scope rather than a full redesign."
    ],
    "compliance_gates": [
      "Protected routes remain authenticated.",
      "Session, share, and artifact operations preserve tenant and user ownership.",
      "Memory/context state does not expose private source text beyond the current user's authorized scope.",
      "UI controls have visible focus states and reachable names for primary commands.",
      "No secret values are rendered in activity, trace, connector, or context panels."
    ],
    "acceptance_gates": [
      "NGA-F010 is passing or blocked with a named missing event/UI contract.",
      "NGA-F011 is passing or blocked with a named session/share/artifact gap.",
      "Browser evidence covers desktop and mobile assistant layouts.",
      "Independent critic evidence and minimal-change scope notes appear in the NGA-04 report."
    ],
    "rollback_plan": [
      "Revert touched assistant UI, focused component, e2e, API-test, and harness files.",
      "Disable new UI state behind existing feature or config flags if a runtime gate fails.",
      "Restore NGA-F010 and NGA-F011 statuses to failing or blocked if browser evidence cannot be recovered."
    ]
  },
  "evidence": {
    "outputs": [
      "docs/general_ai_assistant_next_gen/reports/nga-04-assistant-ux-and-session-experience-report.md",
      "web/test-results"
    ],
    "required_artifacts": [
      "phase report with validation output",
      "progress log entry",
      "feature oracle evidence for NGA-F010 and NGA-F011",
      "continuity ledger code-summary writeback",
      "source packet code facts for assistant UX and session behavior",
      "handoff entry for NGA-05",
      "independent critic evidence and minimal-change scope notes",
      "browser evidence for desktop and mobile assistant states"
    ],
    "waiver_policy": "A skipped browser or live-runtime gate must name the missing server, credential, fixture, or route and record residual user risk.",
    "next_phase_handoff": "NGA-05 may start only after the UI and session surfaces expose enough state for eval, release, and rollback gates."
  },
  "stop_conditions": [
    "Stop if backend event or memory contracts are missing and cannot be mocked truthfully.",
    "Stop if live provider credentials are required.",
    "Stop if protected session/share ownership cannot be validated or blocked precisely.",
    "Stop if the UI task expands into a full redesign outside the assistant surface."
  ]
}
```

## Coding Agent Contract

- PHASE_ID: NGA-04
- GOAL_TARGET: Expose agent state, capabilities, memory/context state, approvals, artifacts, and durable sessions in the assistant UI.
- GOAL_PROMPT: Complete NGA-04 Assistant UX and Session Experience for `.` by following `docs/general_ai_assistant_next_gen/phase-04-assistant-ux-and-session-experience.md`; work on NGA-F010 and NGA-F011; stay inside the named edit boundaries; finish only after validation, regression, browser, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: NGA-03
- READ_FIRST: `docs/general_ai_assistant_next_gen/README.md`, `docs/general_ai_assistant_next_gen/phase-manifest.md`, this file
- PRIMARY_CONTEXT: `web/src/pages/assistant/index.tsx`, `web/src/pages/assistant/sse-events.ts`, `web/src/pages/assistant/types.ts`, `web/src/pages/assistant/hooks/useChatSession.ts`, assistant activity/timeline/connectors/context/customize/share components, `web/src/components/ConversationSidebar.tsx`, assistant Playwright specs, session/share API tests
- LIKELY_EDIT_PATHS: `web/src/pages/assistant/**`, `web/src/components/ConversationSidebar.tsx`, `web/src/components/agent/**`, `web/src/components/artifacts/**`, assistant e2e specs, session/share API tests, `docs/general_ai_assistant_next_gen/**`
- DO_NOT_EDIT: backend event/memory contracts without dependency writeback, database migrations, env files, production data
- EXECUTION_MODE: plan-first; implement one UX/session slice; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `pnpm -C web type-check`; `pnpm -C web lint`; `pnpm -C web build`; `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts web/e2e/assistant-history.spec.ts web/e2e/assistant-memory.spec.ts`; `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/api/test_conversation_share_quiz.py`; `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`
- BROWSER_CHECKS: `/assistant` desktop 1440x900, `/assistant` mobile 390x844, stream event rendering, session resume/share/artifact state.
- REGRESSION_SCOPE: Authenticated assistant route, chat input, file upload, model/KB selector, web search toggle, generated artifact views, prompt suggestions, and conversation sidebar.
- COMPLIANCE_GATES: Auth route protection, tenant/user ownership, memory privacy, focus visibility, no secret rendering.
- ROLLBACK_PLAN: Revert touched frontend/test/harness files and disable new UI state through existing flags if needed.
- ACCEPTANCE_GATES: NGA-F010 and NGA-F011 have evidence or precise blockers; browser evidence covers desktop and mobile; independent critic evidence and minimal-change scope notes are recorded.
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_next_gen/reports/nga-04-assistant-ux-and-session-experience-report.md`
- STOP_CONDITIONS: Stop if backend contracts are missing, live provider credentials are required, session ownership cannot be validated, or scope becomes a full redesign.

## Task Spec

NGA-04 makes the assistant feel like an inspectable agent, not a plain chat box. Users should see what the assistant is doing, which capabilities are active, what memory/context state is in play, where approvals are needed, and how artifacts and sessions survive across work.

## Problem Boundary

This phase does not invent new backend semantics. If backend events or memory fields are missing, use truthful blocked states or mocked fixtures and record the dependency.

## Context Policy

Read the named assistant page, components, hooks, e2e specs, and API tests. Load backend internals only if a UI state cannot be implemented from the published API/event contract.

## Requirements

### R1 Agent State Visibility

The UI renders run lifecycle, tool lifecycle, approvals, context/memory state, and artifact events through compact, scannable surfaces.

### R2 Capability Control

The UI shows selected model, KB, web search, connectors, customization, and capability state in a way that users can inspect before and during a run.

### R3 Durable Sessions

Conversations support resume, history navigation, share, artifact continuity, and feedback states without losing user ownership boundaries.

### R4 Responsive Interaction

Desktop and mobile assistant layouts avoid text overlap, horizontal overflow, and hidden primary controls.

## Test and Regression Requirements

Run frontend typecheck, lint, build, focused Playwright assistant specs, session/share API tests, and strict harness validation.

## Compliance and Safety Requirements

Protected routes remain authenticated. Session/share/artifact ownership remains tenant and user scoped. Memory/context panels must not expose unauthorized source text or secrets.

## Rollback and Recovery

Rollback is a focused revert of assistant UI and test changes. If a UI state depends on missing backend data, record a blocker rather than adding fake product behavior.

## Execution Capture

The report must include screenshots or Playwright artifact paths, command evidence, changed file list, independent critic notes, minimal-change scope, and release-eval handoff.

## Evaluator Protocol

The independent critic checks desktop and mobile layout, absence of incoherent overlap, event-state truthfulness, auth/session ownership, and whether the implementation stayed inside assistant surfaces.

## Critic Protocol

The independent critic must review the actor report, changed files, validation output, browser/runtime evidence when required, feature-oracle evidence, and minimal-change scope from a fresh context. The critic artifact must include `Critic Verdict`, cite the actor report reviewed, and record any residual risk or waiver.

## Acceptance Criteria

- Frontend typecheck, lint, and build pass.
- Focused browser checks pass or record a precise runtime blocker.
- Session/share API tests pass.
- NGA-F010 and NGA-F011 have evidence or precise blockers.
- NGA-05 receives a clear eval/release handoff.

## Risks

- UI can overpromise backend capabilities if events are not available.
- Large assistant redesigns can destabilize existing workflows.
- Session sharing and artifacts can leak data if ownership boundaries are unclear.
