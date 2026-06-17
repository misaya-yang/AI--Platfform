# Phase 04 - Deployment Readiness

> For agentic workers: enter plan-first mode before editing. Execute this phase only, write evidence, and do not deploy until the user explicitly approves deployment.

**Goal:** Prove release readiness, runtime health, rollback, and monitoring gates.

**Architecture:** Deployment uses Docker Compose with gateway and frontend as public entrypoints and assistant-service, knowledge-service, MCP docgen, PostgreSQL, Redis, and Qdrant as internal or local-bound services.

**Tech Stack:** Makefile deployment scripts, Docker Compose, validate-env script, health endpoints, Playwright E2E, service isolation tests.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "hybrid",
  "phase": {
    "id": "GAA-04",
    "number": "04",
    "title": "Deployment Readiness",
    "status": "planned",
    "type": "release",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_upgrade",
    "phase_file": "docs/general_ai_assistant_upgrade/phase-04-deployment-readiness.md",
    "depends_on": ["GAA-03"],
    "unlocks": []
  },
  "goal": {
    "target": "Prove release readiness, runtime health, rollback, and monitoring gates.",
    "prompt": "Complete GAA-04 Deployment Readiness for `.` by following `docs/general_ai_assistant_upgrade/phase-04-deployment-readiness.md`; work on feature-oracle item GAA-F005; validate config, compose runtime, health, browser smoke, microservice isolation, rollback, and monitoring evidence; do not deploy without explicit user approval; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_upgrade/reports/gaa-04-deployment-readiness-plan.md",
    "completion_report": "docs/general_ai_assistant_upgrade/reports/gaa-04-deployment-readiness-report.md"
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
    "read_first": ["docs/general_ai_assistant_upgrade/README.md", "docs/general_ai_assistant_upgrade/phase-manifest.md", "docs/general_ai_assistant_upgrade/loop-contract.json", "docs/general_ai_assistant_upgrade/loop-state.json", "docs/general_ai_assistant_upgrade/feature-oracle.json", "docs/general_ai_assistant_upgrade/progress-log.md", "docs/general_ai_assistant_upgrade/agent-handoff.md", "docs/general_ai_assistant_upgrade/continuity-ledger.md", "docs/general_ai_assistant_upgrade/next-window-prompt.md", "docs/general_ai_assistant_upgrade/source-packet.md", "docs/general_ai_assistant_upgrade/reports/gaa-03-ai-evaluation-and-safety-report.md", "docs/general_ai_assistant_upgrade/phase-04-deployment-readiness.md"],
    "primary_context": ["README.md", "DEPLOY.md", "Makefile", ".env.example", "docker-compose.yml", "docker-compose.dev.yml", "scripts/new/validate-env.sh", "scripts/new/deploy.sh", "scripts/new/status.sh", "web/e2e/site-walkthrough.spec.ts", "tests/integration/test_service_failure_isolation.py", "tests/integration/test_assistant_openapi_contract.py"],
    "context_budget": "focused",
    "do_not_load_unless": ["secret values", "production logs", "provider dashboards", "deployment target shell"]
  },
  "boundaries": {
    "likely_edit_paths": ["README.md", "DEPLOY.md", ".env.example", "docker-compose.yml", "docker-compose.dev.yml", "scripts/new/**", "web/e2e/**", "tests/integration/**", "docs/general_ai_assistant_upgrade/**"],
    "do_not_edit": [".env", "production systems", "database data", "provider dashboards", "DNS settings", "credential stores"],
    "external_inputs": ["Docker engine", "configured .env", "model provider key", "embedding provider key", "E2E user credentials", "user approval for deployment"],
    "secrets_required": ["POSTGRES_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET", "GATEWAY_ASSISTANT_SHARED_SECRET", "chat provider key", "KB_EMBEDDING_API_KEY"]
  },
  "tool_policy": {
    "allowed_tools": ["repo search", "shell validation", "browser automation", "docker compose status", "code review"],
    "approval_required": ["deployment", "production migration", "production data mutation", "DNS or provider dashboard change", "credential rotation", "docker compose down -v"],
    "dangerous_commands": ["git reset --hard", "rm -rf", "docker compose down -v", "DROP SCHEMA", "TRUNCATE", "force push"]
  },
  "risk": {
    "tags": ["release", "database", "schema", "auth", "security", "external-service"],
    "data_mutation": false,
    "migration_required": true,
    "browser_required": true,
    "ai_eval_required": true,
    "external_service_required": true,
    "release_blocking": true
  },
  "validation": {
    "commands": [
      {"id": "compose-static-config", "cwd": ".", "command": "docker compose --env-file .env.example config --quiet", "expected": "command exits 0", "required": true},
      {"id": "config-validation", "cwd": ".", "command": "make validate-config", "expected": "command exits 0 after .env is configured", "required": true},
      {"id": "runtime-validation", "cwd": ".", "command": "make validate", "expected": "command exits 0 against running stack", "required": true},
      {"id": "service-isolation", "cwd": ".", "command": "make test-isolation", "expected": "command exits 0 without unreachable-service skips", "required": true},
      {"id": "frontend-e2e", "cwd": ".", "command": "pnpm -C web e2e -- web/e2e/site-walkthrough.spec.ts", "expected": "command exits 0 with screenshots", "required": true}
    ],
    "browser_checks": ["Frontend http://localhost:8081 loads login page", "Authenticated walkthrough covers visible sidebar routes", "Gateway readiness http://localhost:8080/health/ready returns ready state", "Assistant route stream smoke has no console or page errors"],
    "regression_scope": ["gateway health", "assistant-service health", "knowledge-service health", "docgen health", "PostgreSQL auth", "Redis auth", "Qdrant health", "frontend runtime config", "assistant eval gates from GAA-03"],
    "compliance_gates": ["no secret values printed", "auth domain configured", "bootstrap password rotated for shared deployment", "CORS origins explicit", "infrastructure ports private", "rollback commands documented", "monitoring health endpoints documented"],
    "acceptance_gates": ["GAA-F005 status has runtime evidence", "all required commands pass or report lists blockers", "browser screenshots exist", "rollback plan is command-level", "launch decision is recorded", "review evidence is recorded", "minimal-change scope is documented", "whole-demand regression across completed feature-oracle items is recorded or blocked"],
    "rollback_plan": ["use make stop to stop containers without deleting volumes", "use prior image tag or git revert for app rollback", "do not run docker compose down -v without explicit approval", "database migration rollback must name migration file and backup evidence before execution"]
  },
  "evidence": {
    "outputs": ["docs/general_ai_assistant_upgrade/reports/gaa-04-deployment-readiness-report.md", "web/test-results or Playwright screenshot paths", "docker compose ps output summary"],
    "required_artifacts": ["phase report", "config validation output summary", "runtime health output summary", "browser screenshot paths", "rollback command list", "launch decision", "progress-log entry", "feature-oracle evidence", "continuity-ledger update", "source-packet update", "handoff update"],
    "waiver_policy": "A skipped release gate requires explicit user waiver, reason, and launch risk.",
    "next_phase_handoff": "This is the final release gate; report must state launch, blocked, or waived."
  },
  "stop_conditions": ["GAA-03 report is missing", ".env is absent", "Docker engine is unavailable", "provider keys are absent", "health endpoint fails", "deployment approval is missing", "rollback cannot be described before launch"]
}
```

## Coding Agent Contract

- PHASE_ID: GAA-04
- GOAL_TARGET: Prove release readiness, runtime health, rollback, and monitoring gates.
- GOAL_PROMPT: Complete GAA-04 Deployment Readiness for `.` by following `docs/general_ai_assistant_upgrade/phase-04-deployment-readiness.md`; work on feature-oracle item GAA-F005; validate config, compose runtime, health, browser smoke, microservice isolation, rollback, and monitoring evidence; do not deploy without explicit user approval; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: GAA-03
- READ_FIRST: `docs/general_ai_assistant_upgrade/README.md`, `docs/general_ai_assistant_upgrade/phase-manifest.md`, `reports/gaa-03-ai-evaluation-and-safety-report.md`, this file
- PRIMARY_CONTEXT: `README.md`, `DEPLOY.md`, `Makefile`, `.env.example`, `docker-compose.yml`, `docker-compose.dev.yml`, `scripts/new/validate-env.sh`, `scripts/new/deploy.sh`, `scripts/new/status.sh`, `web/e2e/site-walkthrough.spec.ts`, `tests/integration/test_service_failure_isolation.py`, `tests/integration/test_assistant_openapi_contract.py`
- LIKELY_EDIT_PATHS: `README.md`, `DEPLOY.md`, `.env.example`, `docker-compose.yml`, `docker-compose.dev.yml`, `scripts/new/**`, `web/e2e/**`, `tests/integration/**`, `docs/general_ai_assistant_upgrade/**`
- DO_NOT_EDIT: `.env`, production systems, database data, provider dashboards, DNS settings, credential stores
- EXECUTION_MODE: plan-first; validate config first; request approval before deploy; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `docker compose --env-file .env.example config --quiet`; `make validate-config`; `make validate`; `make test-isolation`; `pnpm -C web e2e -- web/e2e/site-walkthrough.spec.ts`
- BROWSER_CHECKS: frontend login loads; authenticated sidebar walkthrough; gateway readiness ready; assistant stream smoke without console/page errors
- REGRESSION_SCOPE: gateway, assistant-service, knowledge-service, docgen, PostgreSQL, Redis, Qdrant, runtime config, GAA-03 eval gates
- COMPLIANCE_GATES: no secret printing; auth domain set; bootstrap password rotated for shared deployment; CORS explicit; infrastructure ports private; rollback documented; health endpoints documented
- ROLLBACK_PLAN: `make stop`; prior image tag or git revert; no `docker compose down -v` without approval; migration rollback names migration and backup evidence
- ACCEPTANCE_GATES: GAA-F005 has runtime evidence; required commands pass or blockers listed; screenshots exist; rollback is command-level; launch decision recorded; review evidence recorded; minimal-change scope documented; whole-demand regression across completed feature-oracle items recorded or blocked
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_upgrade/reports/gaa-04-deployment-readiness-report.md`
- STOP_CONDITIONS: GAA-03 missing, `.env` absent, Docker unavailable, provider keys absent, health failure, deploy approval missing, rollback missing

## Task Spec

Prove deployment readiness for the completed assistant upgrade. Do not deploy until the user explicitly approves deployment.

## Problem Boundary

This phase validates and documents release readiness. Production launch is a gated operation, not an implied step.

## Context Policy

Read config and scripts. Do not read secret values. Use command summaries that hide credentials.

## Requirements

### R1 Config Readiness

`make validate-config` must pass with `.env` populated.

### R2 Runtime Health

`make validate`, `make status`, health endpoints, and service isolation must pass on the running stack.

### R3 Browser Release Smoke

Authenticated route walkthrough and assistant stream smoke must produce screenshots and no unhandled browser errors.

### R4 Rollback Evidence

Rollback commands and data-safety boundaries must be documented before launch approval.

## Test and Regression Requirements

Execute the required commands in order after prerequisites are available. Record skips as blockers.

## Compliance and Safety Requirements

No secret printing, no destructive volume deletion, no production migration, and no deployment without approval.

## Rollback and Recovery

Prefer `make stop` for non-destructive stop. Use prior image tag or git revert for app rollback. Data-destructive reset requires explicit approval.

## Execution Capture

Write `reports/gaa-04-deployment-readiness-report.md` with command summaries, screenshots, health outputs, rollback plan, and launch decision.

## Evaluator Protocol

Independently verify that no release gate is marked passed without command or screenshot evidence.

## Acceptance Criteria

- Required runtime commands pass or blockers are recorded.
- Browser evidence exists.
- GAA-F005 has evidence.
- Launch decision is explicit.

## Risks

- Missing `.env` or provider keys blocks honest release validation; do not downgrade those blockers into success.
