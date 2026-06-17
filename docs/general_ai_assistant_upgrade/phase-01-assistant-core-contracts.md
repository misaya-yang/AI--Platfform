# Phase 01 - Assistant Core Contracts

> For agentic workers: enter plan-first mode before editing. Execute this phase only, write evidence, and do not advance until acceptance gates pass or blockers are documented.

**Goal:** Upgrade one assistant core contract slice while preserving gateway and service boundaries.

**Architecture:** Gateway routes in `src/api/v1/assistant.py` must remain compatible with assistant-service, shared `ai-gateway-core` primitives, session storage, artifact storage, streaming, internal auth, and tenant isolation.

**Tech Stack:** FastAPI, async pytest, ai-gateway-core, assistant-service, gateway assistant API, service proxy, session and artifact tests.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "hybrid",
  "phase": {
    "id": "GAA-01",
    "number": "01",
    "title": "Assistant Core Contracts",
    "status": "passed",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_upgrade",
    "phase_file": "docs/general_ai_assistant_upgrade/phase-01-assistant-core-contracts.md",
    "depends_on": ["GAA-00"],
    "unlocks": ["GAA-02"]
  },
  "goal": {
    "target": "Upgrade one assistant core contract slice while preserving gateway and service boundaries.",
    "prompt": "Complete GAA-01 Assistant Core Contracts for `.` by following `docs/general_ai_assistant_upgrade/phase-01-assistant-core-contracts.md`; work on feature-oracle item GAA-F002; preserve gateway, assistant-service, tenant, artifact, streaming, and shared-core contracts; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_upgrade/reports/gaa-01-assistant-core-contracts-plan.md",
    "completion_report": "docs/general_ai_assistant_upgrade/reports/gaa-01-assistant-core-contracts-report.md"
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
    "read_first": ["docs/general_ai_assistant_upgrade/README.md", "docs/general_ai_assistant_upgrade/phase-manifest.md", "docs/general_ai_assistant_upgrade/loop-contract.json", "docs/general_ai_assistant_upgrade/loop-state.json", "docs/general_ai_assistant_upgrade/feature-oracle.json", "docs/general_ai_assistant_upgrade/progress-log.md", "docs/general_ai_assistant_upgrade/agent-handoff.md", "docs/general_ai_assistant_upgrade/continuity-ledger.md", "docs/general_ai_assistant_upgrade/next-window-prompt.md", "docs/general_ai_assistant_upgrade/source-packet.md", "docs/general_ai_assistant_upgrade/reports/gaa-00-baseline-release-audit-report.md", "docs/general_ai_assistant_upgrade/phase-01-assistant-core-contracts.md"],
    "primary_context": ["src/api/v1/assistant.py", "src/api/v1/_assistant_proxy.py", "apps/assistant-service/src/assistant_service/main.py", "apps/assistant-service/src/assistant_service/api", "packages/ai-gateway-core/src/ai_gateway_core/storage", "packages/ai-gateway-core/src/ai_gateway_core/session", "packages/ai-gateway-core/src/ai_gateway_core/proxy", "tests/api/test_assistant_sessions.py", "tests/services/assistant", "tests/integration/test_assistant_core_isolation.py"],
    "context_budget": "focused",
    "do_not_load_unless": ["provider dashboards", "secret files", "production logs", "unbounded assistant-service modules outside the selected slice"]
  },
  "boundaries": {
    "likely_edit_paths": ["src/api/v1/assistant.py", "src/api/v1/_assistant_proxy.py", "apps/assistant-service/src/assistant_service/**", "packages/ai-gateway-core/src/ai_gateway_core/**", "tests/api/test_assistant_sessions.py", "tests/services/assistant/**", "tests/integration/test_assistant_core_isolation.py", "docs/general_ai_assistant_upgrade/**"],
    "do_not_edit": [".env", "database/migrations/**", "web/src/pages/**", "docker-compose.yml", "production systems", "provider dashboards"],
    "external_inputs": ["mock provider responses", "local pytest fixtures"],
    "secrets_required": []
  },
  "tool_policy": {
    "allowed_tools": ["repo search", "shell validation", "apply_patch", "code review", "code simplifier"],
    "approval_required": ["new dependency", "schema migration", "deployment", "production data mutation", "external provider change"],
    "dangerous_commands": ["git reset --hard", "rm -rf", "docker compose down -v", "DROP SCHEMA", "TRUNCATE"]
  },
  "risk": {
    "tags": ["api", "ai", "agent", "security"],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": false,
    "ai_eval_required": true,
    "external_service_required": false,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {"id": "assistant-session-artifact-tests", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py", "expected": "command exits 0", "required": true},
      {"id": "assistant-core-isolation", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov tests/integration/test_assistant_core_isolation.py", "expected": "command exits 0", "required": true},
      {"id": "assistant-service-targets", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_assistant_service.py tests/services/assistant/test_message_persistence.py tests/services/assistant/test_request_id_propagation.py", "expected": "command exits 0", "required": true},
      {"id": "assistant-ruff", "cwd": ".", "command": "uv run ruff check src/api/v1/assistant.py apps/assistant-service/src/assistant_service packages/ai-gateway-core/src/ai_gateway_core tests/api/test_assistant_sessions.py tests/services/assistant", "expected": "command exits 0 for touched files or report lists pre-existing lint blockers", "required": true}
    ],
    "browser_checks": ["No browser route is changed in this phase; API/runtime evidence comes from pytest targets."],
    "regression_scope": ["tenant isolation", "session ownership checks", "artifact listing/download behavior", "assistant streaming routes", "request-id propagation", "gateway-to-assistant proxy behavior"],
    "compliance_gates": ["auth checks preserve user and tenant boundaries", "no secret values in logs or reports", "provider calls remain mockable", "unexpected storage/provider errors are not swallowed silently"],
    "acceptance_gates": ["GAA-F002 status has evidence", "phase report names changed API contracts", "all required commands pass or blockers are documented", "continuity ledger records downstream UI impact", "review evidence is recorded", "minimal-change scope is documented"],
    "rollback_plan": ["revert assistant-core code and tests touched in this phase", "restore prior API response schema if contract tests fail", "leave documentation updates with blocker status if code revert is required"]
  },
  "evidence": {
    "outputs": ["docs/general_ai_assistant_upgrade/reports/gaa-01-assistant-core-contracts-report.md"],
    "required_artifacts": ["phase report", "pytest output summary", "ruff output summary", "progress-log entry", "feature-oracle evidence", "continuity-ledger update", "source-packet update", "handoff update"],
    "waiver_policy": "A skipped assistant check must list the missing dependency and downstream UI or release impact.",
    "next_phase_handoff": "Unlock GAA-02 only after assistant API/runtime contracts are recorded."
  },
  "stop_conditions": ["selected assistant contract cannot be isolated", "auth or tenant behavior cannot be tested", "schema migration is required", "external provider credentials are required", "edits outside likely paths are required"]
}
```

## Coding Agent Contract

- PHASE_ID: GAA-01
- GOAL_TARGET: Upgrade one assistant core contract slice while preserving gateway and service boundaries.
- GOAL_PROMPT: Complete GAA-01 Assistant Core Contracts for `.` by following `docs/general_ai_assistant_upgrade/phase-01-assistant-core-contracts.md`; work on feature-oracle item GAA-F002; preserve gateway, assistant-service, tenant, artifact, streaming, and shared-core contracts; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: GAA-00
- READ_FIRST: `docs/general_ai_assistant_upgrade/README.md`, `docs/general_ai_assistant_upgrade/phase-manifest.md`, `docs/general_ai_assistant_upgrade/reports/gaa-00-baseline-release-audit-report.md`, this file
- PRIMARY_CONTEXT: `src/api/v1/assistant.py`, `src/api/v1/_assistant_proxy.py`, `apps/assistant-service/src/assistant_service/main.py`, `apps/assistant-service/src/assistant_service/api`, `packages/ai-gateway-core/src/ai_gateway_core/storage`, `packages/ai-gateway-core/src/ai_gateway_core/session`, `packages/ai-gateway-core/src/ai_gateway_core/proxy`, `tests/api/test_assistant_sessions.py`, `tests/services/assistant`, `tests/integration/test_assistant_core_isolation.py`
- LIKELY_EDIT_PATHS: `src/api/v1/assistant.py`, `src/api/v1/_assistant_proxy.py`, `apps/assistant-service/src/assistant_service/**`, `packages/ai-gateway-core/src/ai_gateway_core/**`, `tests/api/test_assistant_sessions.py`, `tests/services/assistant/**`, `tests/integration/test_assistant_core_isolation.py`, `docs/general_ai_assistant_upgrade/**`
- DO_NOT_EDIT: `.env`, `database/migrations/**`, `web/src/pages/**`, `docker-compose.yml`, production systems, provider dashboards
- EXECUTION_MODE: plan-first; implement stepwise; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py`; `uv run --extra dev --extra test pytest -q --no-cov tests/integration/test_assistant_core_isolation.py`; `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_assistant_service.py tests/services/assistant/test_message_persistence.py tests/services/assistant/test_request_id_propagation.py`; `uv run ruff check ...`
- BROWSER_CHECKS: no browser route changed in this phase; API/runtime evidence comes from pytest targets
- REGRESSION_SCOPE: tenant isolation, session ownership, artifacts, assistant streaming, request-id propagation, gateway-to-assistant proxy behavior
- COMPLIANCE_GATES: preserve auth and tenant boundaries; no secret values; provider calls remain mockable; unexpected storage/provider errors are not silently swallowed
- ROLLBACK_PLAN: revert phase-scoped assistant-core code and tests; restore prior API response schema if contract tests fail
- ACCEPTANCE_GATES: GAA-F002 has evidence; report names changed contracts; commands pass or blockers documented; continuity ledger records downstream UI impact; review evidence recorded; minimal-change scope documented
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_upgrade/reports/gaa-01-assistant-core-contracts-report.md`
- STOP_CONDITIONS: contract cannot be isolated, auth cannot be tested, migration required, provider credentials required, edit paths must expand

## Task Spec

Select one bounded assistant-core contract slice, implement it, and record evidence. Do not start frontend or release work in this phase.

## Problem Boundary

The phase is limited to assistant API/runtime contracts. UI layout, browser walkthrough, deployment commands, and production configuration belong to later phases.

## Context Policy

Read only files named in `PRIMARY_CONTEXT` before planning. Add more context only after recording why the selected contract requires it.

## Requirements

### R1 Contract Preservation

Gateway requests, assistant-service behavior, artifacts, sessions, streaming, and shared-core helpers must retain documented response shapes and failure behavior.

### R2 Tenant and Auth Safety

User and tenant checks must remain explicit for session and artifact access.

### R3 Mockable AI Runtime

Provider or tool behavior must have deterministic test doubles.

## Test and Regression Requirements

Run the required pytest targets and ruff command. Add focused tests for any changed assistant contract.

## Compliance and Safety Requirements

No secret values, production traffic, provider dashboard changes, or schema migration execution.

## Rollback and Recovery

Revert only the assistant-core files touched in this phase and leave a blocker report if validation cannot pass.

## Execution Capture

Write `reports/gaa-01-assistant-core-contracts-report.md` with changed files, command summaries, skipped gates, and unlock decision.

## Evaluator Protocol

Review auth paths, tenant scoping, streaming behavior, artifact access, service proxy contracts, and error handling before accepting completion.

## Acceptance Criteria

- Required commands pass or have blocker evidence.
- GAA-F002 has evidence.
- GAA-02 receives a clear UI contract handoff.

## Risks

- A small API change can break `/assistant` UI state or streaming; record every response-shape change in the ledger.
