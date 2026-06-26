# Phase 02 - Skills and MCP Capability Layer

> For agentic workers: execute this phase only after NGA-01 passes or is explicitly waived in its report. Work on NGA-F004, NGA-F005, and NGA-F006.

**Goal:** Make skills and MCP a safe, discoverable, progressively loaded capability layer on top of the canonical assistant harness.

**Architecture:** NGA-02 attaches tools to the agent harness through `SkillToolBridge`, runtime skill registry, MCP manager/client/config, tenant MCP policy, tool selector, tool invoker, connector registry, and tool audit records. The phase should remove ambiguity around default tenant MCP access and generated skill enablement without building an unrestricted self-modifying system.

**Tech Stack:** assistant-service Python runtime, skill manifests, MCP client/manager, tenant MCP config, tool registry/selector/invoker, audit logging, pytest, ruff, optional frontend connector surface checks.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "execution",
  "phase": {
    "id": "NGA-02",
    "number": "02",
    "title": "Skills and MCP Capability Layer",
    "status": "planned",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_next_gen",
    "phase_file": "docs/general_ai_assistant_next_gen/phase-02-skills-and-mcp-capability-layer.md",
    "depends_on": [
      "NGA-01"
    ],
    "unlocks": [
      "NGA-03"
    ]
  },
  "goal": {
    "target": "Make skills and MCP discoverable, tenant-scoped, risk-labelled, auditable, and progressively loaded.",
    "prompt": "Complete NGA-02 Skills and MCP Capability Layer for `.` by following `docs/general_ai_assistant_next_gen/phase-02-skills-and-mcp-capability-layer.md`; work on NGA-F004, NGA-F005, and NGA-F006; stay inside the named assistant-service, focused frontend, test, and harness boundaries; finish only after validation, regression, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-plan.md",
    "completion_report": "docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-report.md"
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
      "docs/general_ai_assistant_next_gen/phase-02-skills-and-mcp-capability-layer.md"
    ],
    "primary_context": [
      "apps/assistant-service/src/assistant_service/core/runtime/skills/registry.py",
      "apps/assistant-service/src/assistant_service/core/runtime/skills/builder.py",
      "apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py",
      "apps/assistant-service/src/assistant_service/core/skills/executor.py"
    ],
    "context_budget": "focused",
    "do_not_load_unless": [
      "provider dashboards",
      "live MCP servers outside local fixtures",
      "database migrations",
      ".env files",
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
      "apps/assistant-service/src/assistant_service/core/runtime/skills/**",
      "apps/assistant-service/src/assistant_service/core/skills/**",
      "apps/assistant-service/src/assistant_service/core/mcp/**",
      "apps/assistant-service/src/assistant_service/core/tools/tool_selector.py",
      "apps/assistant-service/src/assistant_service/core/tool_invoker.py",
      "apps/assistant-service/src/assistant_service/core/audit/tool_audit.py",
      "apps/assistant-service/src/assistant_service/api/routes/tools.py",
      "tests/services/assistant/tools/**",
      "tests/services/assistant/test_tool_dedup.py",
      "tests/services/assistant/test_tool_result_formatter.py",
      "web/src/pages/assistant/components/ConnectorsPanel.tsx",
      "docs/general_ai_assistant_next_gen/**"
    ],
    "do_not_edit": [
      "database/** without a migration plan and user approval",
      "provider secrets",
      "production connector configs",
      "unrelated document generation internals",
      "runtime event contract from NGA-01 without recording a dependency issue"
    ],
    "external_inputs": [
      "Local mocked MCP server fixtures or existing mock-mcp-servers only.",
      "No live connector credential is required."
    ],
    "secrets_required": []
  },
  "tool_policy": {
    "allowed_tools": [
      "rg",
      "sed",
      "apply_patch",
      "uv pytest",
      "ruff",
      "pnpm type-check",
      "harness validator"
    ],
    "approval_required": [
      "live MCP connector credential use",
      "new database table or migration",
      "external connector enablement",
      "deployment",
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
      "ai",
      "agent",
      "eval",
      "external-service",
      "security",
      "frontend"
    ],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": true,
    "ai_eval_required": true,
    "external_service_required": true,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {
        "id": "skills-mcp-pytest",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py",
        "expected": "Connector registry, context tools, primitive tools, tool deduplication, and result formatting tests pass.",
        "required": true
      },
      {
        "id": "skills-mcp-ruff",
        "cwd": ".",
        "command": "uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py",
        "expected": "Touched skills, MCP, tool, audit, API, and focused test files pass ruff.",
        "required": true
      },
      {
        "id": "connectors-panel-typecheck",
        "cwd": ".",
        "command": "pnpm -C web type-check",
        "expected": "Frontend connector-surface changes type-check successfully.",
        "required": true
      },
      {
        "id": "strict-harness-validation",
        "cwd": ".",
        "command": "python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score",
        "expected": "Harness remains strict-validator clean after NGA-02 evidence writeback.",
        "required": true
      }
    ],
    "browser_checks": [
      "If `ConnectorsPanel.tsx` or assistant capability UI changes, run `E2E_REUSE_SERVER=1 E2E_BASE_URL=http://127.0.0.1:8081 E2E_API_URL=http://127.0.0.1:8080 pnpm -C web exec playwright test web/e2e/chat-experience.spec.ts` and capture the `/assistant` connector state.",
      "If no frontend change is made, record that the connector browser check is not applicable in the NGA-02 report."
    ],
    "regression_scope": [
      "Skill selection remains token-efficient and does not inject every skill into every prompt.",
      "MCP tool descriptions remain sanitized and bounded.",
      "Tenant MCP defaults are explicit and tested.",
      "Generated skills remain propose-review-test-enable, not automatic execution.",
      "Independent critic evidence confirms the phase uses a minimal-change scope."
    ],
    "compliance_gates": [
      "MCP tool calls must preserve tenant, user, and policy metadata.",
      "MCP resources and generated skill files must not bypass approval, sandbox, or tool-risk labels.",
      "External connector failures must produce observable errors without leaking credentials.",
      "Generated procedural memory must not become enabled without independent critic evidence, eval evidence, and rollback metadata."
    ],
    "acceptance_gates": [
      "NGA-F004 is passing or blocked with a named missing skills contract.",
      "NGA-F005 is passing or blocked with a named MCP tenant-policy gap.",
      "NGA-F006 is passing or blocked with a named procedural-memory safety gap.",
      "Skills and MCP attach to the NGA-01 run event contract.",
      "Independent critic evidence and minimal-change scope notes appear in the NGA-02 report."
    ],
    "rollback_plan": [
      "Revert touched skills, MCP, tool, audit, API, focused frontend, and focused test files.",
      "Disable newly introduced skill or MCP capability flags if a runtime gate fails.",
      "Restore NGA-F004, NGA-F005, and NGA-F006 statuses to failing or blocked if validation cannot be recovered."
    ]
  },
  "evidence": {
    "outputs": [
      "docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-report.md"
    ],
    "required_artifacts": [
      "phase report with validation output",
      "progress log entry",
      "feature oracle evidence for NGA-F004, NGA-F005, and NGA-F006",
      "continuity ledger code-summary writeback",
      "source packet code facts for skills and MCP boundaries",
      "handoff entry for NGA-03",
      "independent critic evidence and minimal-change scope notes"
    ],
    "waiver_policy": "A skipped live connector gate must name the missing credential, mock path, and residual risk.",
    "next_phase_handoff": "NGA-03 may start only after skills and MCP retrieval outputs are bounded enough for memory, RAG, and context budgeting."
  },
  "stop_conditions": [
    "Stop if a live connector credential is required.",
    "Stop if a database migration is required without approval.",
    "Stop if a generated skill would run without review and tests.",
    "Stop if the phase needs to change the NGA-01 event contract without reopening that dependency."
  ]
}
```

## Coding Agent Contract

- PHASE_ID: NGA-02
- GOAL_TARGET: Make skills and MCP discoverable, tenant-scoped, risk-labelled, auditable, and progressively loaded.
- GOAL_PROMPT: Complete NGA-02 Skills and MCP Capability Layer for `.` by following `docs/general_ai_assistant_next_gen/phase-02-skills-and-mcp-capability-layer.md`; work on NGA-F004, NGA-F005, and NGA-F006; stay inside the named edit boundaries; finish only after validation, regression, browser, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: NGA-01
- READ_FIRST: `docs/general_ai_assistant_next_gen/README.md`, `docs/general_ai_assistant_next_gen/phase-manifest.md`, this file
- PRIMARY_CONTEXT: `apps/assistant-service/src/assistant_service/core/runtime/skills/registry.py`, `apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py`, `apps/assistant-service/src/assistant_service/core/skills/builtin/skill_create.py`, `apps/assistant-service/src/assistant_service/core/mcp/manager.py`, `apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py`, `apps/assistant-service/src/assistant_service/core/tools/tool_selector.py`, `apps/assistant-service/src/assistant_service/core/tool_invoker.py`, `apps/assistant-service/src/assistant_service/core/audit/tool_audit.py`, `apps/assistant-service/src/assistant_service/api/routes/tools.py`, `web/src/pages/assistant/components/ConnectorsPanel.tsx`, focused tool tests
- LIKELY_EDIT_PATHS: skills runtime, MCP runtime, tool selector/invoker, tool audit, tools API route, connector panel, focused tests, `docs/general_ai_assistant_next_gen/**`
- DO_NOT_EDIT: database schema without approval, env files, production connector configs, unrelated docgen internals, NGA-01 event contract without dependency writeback
- EXECUTION_MODE: plan-first; implement one capability-layer slice; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py`; `uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py`; `pnpm -C web type-check`; `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`
- BROWSER_CHECKS: If connector UI changes, run `/assistant` Playwright chat experience with connector state evidence; otherwise record no frontend diff.
- REGRESSION_SCOPE: Skill progressive disclosure, MCP tenant scoping, tool result formatting, tool deduplication, connector UI type safety, and generated-skill review gates.
- COMPLIANCE_GATES: Preserve tenant policy; redact credentials; gate live connectors; prevent unreviewed generated skills.
- ROLLBACK_PLAN: Revert touched files, disable new capability flags, and restore oracle statuses if validation fails.
- ACCEPTANCE_GATES: NGA-F004, NGA-F005, and NGA-F006 have evidence or precise blockers; skills and MCP attach to the NGA-01 run contract; independent critic evidence and minimal-change scope notes are recorded.
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-report.md`
- STOP_CONDITIONS: Stop if live credentials, unapproved migration, unreviewed generated-skill execution, or NGA-01 contract changes are required.

## Task Spec

NGA-02 makes capability expansion safe and understandable. Skills should behave like reviewed procedural packages with progressive disclosure. MCP should behave like a tenant-scoped external tool bridge with explicit risk labels, audit records, and bounded outputs.

## Problem Boundary

This phase does not rebuild the whole assistant UI or memory layer. It defines and implements the capability boundary that later memory, RAG, context, and UX phases inherit.

## Context Policy

Read the named skills, MCP, tool, audit, API, and focused UI files. Do not load connector credentials, production MCP configs, or env files.

## Requirements

### R1 Skills Discovery and Loading

Skills are discoverable through a catalog or registry, selected through a token-aware path, and loaded only when triggered or relevant.

### R2 MCP Tenant and Risk Boundary

MCP tools are tenant-scoped, risk-labelled, auditable, and routed through the same tool invocation context as native tools.

### R3 Procedural Memory Safety

Repeated workflows can be proposed as skills, but enablement requires review, tests, eval evidence, and rollback metadata.

### R4 User and Operator Visibility

The assistant surface or API exposes enough connector/capability state for users and operators to understand what is available and why a tool was blocked.

## Test and Regression Requirements

Run focused pytest, focused ruff, web type-check, and strict harness validation. Add tests for any changed skill, MCP, or generated-skill behavior.

## Compliance and Safety Requirements

No live connector credentials are required. Generated skills cannot self-enable. MCP resource outputs must be treated as untrusted data.

## Rollback and Recovery

Rollback is a focused revert plus disabling newly introduced capability flags. If tenant policy remains unresolved, keep NGA-F005 blocked and record the next action.

## Execution Capture

The report must include capability contract summary, command evidence, independent critic notes, minimal-change scope, and a handoff describing how memory/RAG should consume tool and MCP outputs.

## Evaluator Protocol

The independent critic checks that tool boundaries are not bypassed, progressive disclosure remains token-bounded, generated skills remain critic-gated, and connector UI changes have browser or typecheck evidence.

## Critic Protocol

The independent critic must review the actor report, changed files, validation output, browser/runtime evidence when required, feature-oracle evidence, and minimal-change scope from a fresh context. The critic artifact must include `Critic Verdict`, cite the actor report reviewed, and record any residual risk or waiver.

## Acceptance Criteria

- Focused tool tests pass.
- Focused ruff passes or out-of-scope lint failures are named.
- Web type-check passes when connector UI is touched.
- NGA-F004 through NGA-F006 have evidence or precise blockers.
- NGA-03 handoff explains how memory and context should treat skills and MCP outputs.

## Risks

- Default-open MCP policy can create tenant isolation issues.
- Generated skills can become unsafe if review gates are weak.
- Loading every skill or tool every turn can bloat context and reduce agent quality.
