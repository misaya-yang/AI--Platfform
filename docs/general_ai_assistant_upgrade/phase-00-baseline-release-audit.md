# Phase 00 - Baseline Release Audit

> For agentic workers: enter plan-first mode before editing. Execute this phase only, write evidence, and do not advance until acceptance gates pass or blockers are documented.

**Goal:** Establish verified release and code baseline for the assistant upgrade.

**Architecture:** This phase records the current FastAPI gateway, internal assistant-service, knowledge-service, shared core package, React frontend, Docker compose, and validation boundaries that later phases inherit.

**Tech Stack:** Python 3.13 local venv, FastAPI, pytest, ruff, React 19, Vite 8, pnpm 10, Docker Compose.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "hybrid",
  "phase": {
    "id": "GAA-00",
    "number": "00",
    "title": "Baseline Release Audit",
    "status": "passed",
    "type": "baseline",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_upgrade",
    "phase_file": "docs/general_ai_assistant_upgrade/phase-00-baseline-release-audit.md",
    "depends_on": [],
    "unlocks": ["GAA-01"]
  },
  "goal": {
    "target": "Establish verified release and code baseline for the assistant upgrade.",
    "prompt": "Complete GAA-00 Baseline Release Audit for `.` by following `docs/general_ai_assistant_upgrade/phase-00-baseline-release-audit.md`; work on feature-oracle item GAA-F001; update source packet, continuity ledger, progress log, phase report, and oracle evidence; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_upgrade/reports/gaa-00-baseline-release-audit-plan.md",
    "completion_report": "docs/general_ai_assistant_upgrade/reports/gaa-00-baseline-release-audit-report.md"
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
    "read_first": ["docs/general_ai_assistant_upgrade/README.md", "docs/general_ai_assistant_upgrade/phase-manifest.md", "docs/general_ai_assistant_upgrade/loop-contract.json", "docs/general_ai_assistant_upgrade/loop-state.json", "docs/general_ai_assistant_upgrade/feature-oracle.json", "docs/general_ai_assistant_upgrade/progress-log.md", "docs/general_ai_assistant_upgrade/agent-handoff.md", "docs/general_ai_assistant_upgrade/continuity-ledger.md", "docs/general_ai_assistant_upgrade/next-window-prompt.md", "docs/general_ai_assistant_upgrade/source-packet.md", "docs/general_ai_assistant_upgrade/phase-00-baseline-release-audit.md"],
    "primary_context": ["README.md", "DEPLOY.md", "Makefile", "pyproject.toml", "docker-compose.yml", "docker-compose.dev.yml", "web/package.json", "web/src/router.tsx", "web/e2e/site-walkthrough.spec.ts", "src/api/v1/assistant.py", "packages/ai-gateway-core/src/ai_gateway_core/proxy/drain.py"],
    "context_budget": "focused",
    "do_not_load_unless": ["production logs", "secret files", "provider dashboards"]
  },
  "boundaries": {
    "likely_edit_paths": ["docs/general_ai_assistant_upgrade/**", "src/api/v1/assistant.py", "web/docker-entrypoint.d/40-runtime-config.sh"],
    "do_not_edit": [".env", "production systems", "database data", "provider dashboards", "unrelated SDK packages"],
    "external_inputs": ["local command output", "Docker compose static config output"],
    "secrets_required": []
  },
  "tool_policy": {
    "allowed_tools": ["repo search", "shell validation", "apply_patch", "code review", "code simplifier"],
    "approval_required": ["deployment", "production migration", "production data mutation", "credential rotation", "destructive Git commands"],
    "dangerous_commands": ["git reset --hard", "rm -rf", "docker compose down -v", "DROP SCHEMA", "TRUNCATE"]
  },
  "risk": {
    "tags": ["release", "security"],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": false,
    "ai_eval_required": false,
    "external_service_required": false,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {"id": "backend-full-suite", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov", "expected": "command exits 0 with pytest summary evidence", "required": true},
      {"id": "frontend-typecheck", "cwd": ".", "command": "pnpm -C web type-check", "expected": "command exits 0", "required": true},
      {"id": "frontend-build", "cwd": ".", "command": "pnpm -C web build", "expected": "command exits 0 and route chunks compile", "required": true},
      {"id": "frontend-lint", "cwd": ".", "command": "pnpm -C web lint", "expected": "command exits 0 and warnings are recorded", "required": true},
      {"id": "compose-static-config", "cwd": ".", "command": "docker compose --env-file .env.example config --quiet", "expected": "command exits 0", "required": true}
    ],
    "browser_checks": ["Frontend route chunks compile through pnpm build; authenticated browser walkthrough is assigned to GAA-02."],
    "regression_scope": ["existing backend suite", "frontend compile pipeline", "compose interpolation", "assistant artifact schema tolerance", "drain signal chaining"],
    "compliance_gates": ["no secrets printed", "no production deployment", "no data mutation", "auth and tenant boundaries recorded for later phases"],
    "acceptance_gates": ["GAA-00 report exists", "source packet records validation evidence", "continuity ledger records interface boundaries", "feature oracle GAA-F001 is passing with evidence", "review evidence is recorded", "minimal-change scope is documented"],
    "rollback_plan": ["revert scoped edits in src/api/v1/assistant.py and web/docker-entrypoint.d/40-runtime-config.sh if target tests fail", "remove docs/general_ai_assistant_upgrade only if the user cancels the harness"]
  },
  "evidence": {
    "outputs": ["docs/general_ai_assistant_upgrade/reports/gaa-00-baseline-release-audit-report.md"],
    "required_artifacts": ["phase report", "progress-log entry", "feature-oracle evidence", "continuity-ledger update", "source-packet update", "handoff update"],
    "waiver_policy": "A skipped live runtime gate must list the missing environment item and dependent phase impact.",
    "next_phase_handoff": "GAA-01 is unlocked for assistant core contract work."
  },
  "stop_conditions": ["backend full suite fails", "frontend build fails", "compose static config fails", "secret value is required", "deployment is requested without approval"]
}
```

## Coding Agent Contract

- PHASE_ID: GAA-00
- GOAL_TARGET: Establish verified release and code baseline for the assistant upgrade.
- GOAL_PROMPT: Complete GAA-00 Baseline Release Audit for `.` by following `docs/general_ai_assistant_upgrade/phase-00-baseline-release-audit.md`; work on feature-oracle item GAA-F001; update source packet, continuity ledger, progress log, phase report, and oracle evidence; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: none
- READ_FIRST: `docs/general_ai_assistant_upgrade/README.md`, `docs/general_ai_assistant_upgrade/phase-manifest.md`, this file
- PRIMARY_CONTEXT: `README.md`, `DEPLOY.md`, `Makefile`, `pyproject.toml`, `docker-compose.yml`, `docker-compose.dev.yml`, `web/package.json`, `web/src/router.tsx`, `web/e2e/site-walkthrough.spec.ts`, `src/api/v1/assistant.py`, `packages/ai-gateway-core/src/ai_gateway_core/proxy/drain.py`
- LIKELY_EDIT_PATHS: `docs/general_ai_assistant_upgrade/**`, `src/api/v1/assistant.py`, `web/docker-entrypoint.d/40-runtime-config.sh`
- DO_NOT_EDIT: `.env`, production systems, database data, provider dashboards, unrelated SDK packages
- EXECUTION_MODE: plan-first; implement stepwise; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov`; `pnpm -C web type-check`; `pnpm -C web build`; `pnpm -C web lint`; `docker compose --env-file .env.example config --quiet`
- BROWSER_CHECKS: route chunks compile through frontend build; authenticated browser walkthrough belongs to GAA-02
- REGRESSION_SCOPE: backend suite, frontend compile pipeline, compose interpolation, assistant artifact schema tolerance, drain signal chaining
- COMPLIANCE_GATES: no secret printing, no production deploy, no data mutation, auth and tenant boundaries recorded
- ROLLBACK_PLAN: revert scoped code edits if target tests fail; keep harness unless user cancels it
- ACCEPTANCE_GATES: phase report exists; validation evidence recorded; source packet and continuity ledger updated; GAA-F001 passing with evidence; review evidence recorded; minimal-change scope documented
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_upgrade/reports/gaa-00-baseline-release-audit-report.md`
- STOP_CONDITIONS: backend suite failure, frontend build failure, compose static config failure, secret value requirement, deployment request without approval

## Task Spec

Record the release baseline and unblock GAA-01. This phase is complete for the current session.

## Problem Boundary

This phase does not implement assistant product behavior. It establishes proof of current state and names blockers for live runtime checks.

## Context Policy

Load only listed context files. Do not inspect `.env`, shell history, provider dashboards, or production logs.

## Requirements

### R1 Baseline Evidence

Capture backend, frontend, compose, lint, and targeted microservice validation results.

### R2 Boundary Writeback

Write service boundaries and blocked runtime checks into `source-packet.md` and `continuity-ledger.md`.

## Test and Regression Requirements

The commands listed in `VALIDATION_COMMANDS` must be recorded in the phase report with pass or blocker status.

## Compliance and Safety Requirements

No secrets, deployments, production mutations, destructive Git operations, or data deletion are allowed.

## Rollback and Recovery

If baseline code edits fail validation, revert only the touched code files and rerun the scoped checks.

## Execution Capture

The report at `reports/gaa-00-baseline-release-audit-report.md` is the durable evidence.

## Evaluator Protocol

Confirm the report evidence matches command output and that live runtime blockers are not mislabeled as passed checks.

## Acceptance Criteria

- GAA-F001 is passing with report evidence.
- GAA-01 is named as the next phase.
- Runtime blockers are visible in source packet, progress log, and report.

## Risks

- Full browser and live service checks remain blocked until `.env` and compose runtime are available.
