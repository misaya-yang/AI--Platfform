# Phase 05 - Evaluation Safety and Release Gate

> For agentic workers: execute this phase only after NGA-04 passes or is explicitly waived in its report. Work on NGA-F012 and run whole-demand regression across completed oracle items.

**Goal:** Prove the upgraded assistant is safe, observable, deployable, rollback-ready, and evaluated across the completed next-generation requirements.

**Architecture:** NGA-05 is the terminal gate. It combines assistant golden tests, guardrails, tool safety, service isolation, frontend assistant smoke, compose validation, env validation, release notes, rollback records, and the whole-demand regression matrix for NGA-F001 through NGA-F012.

**Tech Stack:** pytest, ruff, Playwright, pnpm, Docker Compose config validation, Makefile validation targets, release docs, PRD harness validator, and sanitized env-file gates.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "execution",
  "phase": {
    "id": "NGA-05",
    "number": "05",
    "title": "Evaluation Safety and Release Gate",
    "status": "planned",
    "type": "release",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_next_gen",
    "phase_file": "docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md",
    "depends_on": [
      "NGA-04"
    ],
    "unlocks": []
  },
  "goal": {
    "target": "Run eval, safety, deployment, rollback, and whole-demand regression gates for the upgraded assistant.",
    "prompt": "Complete NGA-05 Evaluation Safety and Release Gate for `.` by following `docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md`; work on NGA-F012; run whole-demand regression across completed oracle items; stay inside named test, config, release-doc, and harness boundaries; finish only after validation, regression, browser, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-plan.md",
    "completion_report": "docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md"
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
      "docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md"
    ],
    "primary_context": [
      "tests/services/assistant/test_eval_safety_contracts.py",
      "tests/services/assistant/test_guardrails.py",
      "tests/services/assistant/test_safe_fetch.py",
      "tests/services/assistant/test_safe_fetch_callsites.py"
    ],
    "context_budget": "focused",
    "do_not_load_unless": [
      "real .env values",
      "production deployment logs",
      "package registry credentials",
      "cloud dashboards",
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
      "tests/services/assistant/**",
      "tests/integration/test_assistant_openapi_contract.py",
      "tests/integration/test_assistant_core_isolation.py",
      "tests/integration/test_service_failure_isolation.py",
      "web/e2e/chat-experience.spec.ts",
      "web/e2e/site-walkthrough.spec.ts",
      "README.md",
      "RELEASE.md",
      "docs/general_ai_assistant_next_gen/**"
    ],
    "do_not_edit": [
      "real .env files",
      "production deployment targets",
      "package registry credentials",
      "database migrations without approval",
      "git history"
    ],
    "external_inputs": [
      "Optional external env path: /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env",
      "Use env validation scripts without printing secret values."
    ],
    "secrets_required": [
      "Optional existing env file path only; do not print values."
    ]
  },
  "tool_policy": {
    "allowed_tools": [
      "rg",
      "sed",
      "apply_patch",
      "uv pytest",
      "ruff",
      "pnpm type-check",
      "pnpm lint",
      "pnpm build",
      "Playwright",
      "docker compose config",
      "make validate-config",
      "make validate",
      "harness validator"
    ],
    "approval_required": [
      "deployment",
      "package publish",
      "production migration",
      "credential rotation",
      "destructive git operations"
    ],
    "dangerous_commands": [
      "git reset --hard",
      "git push --force",
      "docker compose down -v",
      "database DROP or TRUNCATE",
      "package publish commands"
    ]
  },
  "risk": {
    "tags": [
      "ai",
      "agent",
      "eval",
      "release",
      "external-service",
      "security",
      "frontend"
    ],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": true,
    "ai_eval_required": true,
    "external_service_required": true,
    "release_blocking": true
  },
  "validation": {
    "commands": [
      {
        "id": "assistant-safety-pytest",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_eval_safety_contracts.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py tests/services/assistant/test_tool_orchestrator.py",
        "expected": "Assistant eval-safety, guardrail, safe-fetch, callsite, and tool orchestration tests pass.",
        "required": true
      },
      {
        "id": "assistant-integration-pytest",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/integration/test_assistant_openapi_contract.py tests/integration/test_assistant_core_isolation.py tests/integration/test_service_failure_isolation.py",
        "expected": "Assistant OpenAPI, core isolation, and service failure-isolation tests pass or record environment-specific skips.",
        "required": true
      },
      {
        "id": "frontend-release-checks",
        "cwd": ".",
        "command": "pnpm -C web type-check && pnpm -C web lint && pnpm -C web build && pnpm -C web e2e:opensource",
        "expected": "Frontend typecheck, lint, build, and open-source route smoke pass.",
        "required": true
      },
      {
        "id": "compose-config",
        "cwd": ".",
        "command": "docker compose --env-file .env.example config --quiet",
        "expected": "Docker Compose static config validates with committed example env values.",
        "required": true
      },
      {
        "id": "external-env-config-gate",
        "cwd": ".",
        "command": "make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env",
        "expected": "External env config validation passes, or the report records the exact missing variable names without secret values.",
        "required": true
      },
      {
        "id": "external-env-runtime-gate",
        "cwd": ".",
        "command": "make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env",
        "expected": "Runtime validation passes, or the report records the exact release blocker without secret values.",
        "required": true
      },
      {
        "id": "strict-harness-validation",
        "cwd": ".",
        "command": "python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score",
        "expected": "Harness remains strict-validator clean after NGA-05 evidence writeback.",
        "required": true
      }
    ],
    "browser_checks": [
      "Run `pnpm -C web e2e:opensource` for public route smoke.",
      "Run `/assistant` focused browser smoke from NGA-04 if assistant UI changed during the terminal phase.",
      "Capture Playwright artifact paths for failed or passed assistant release checks."
    ],
    "regression_scope": [
      "Whole-demand regression covers NGA-F001 through NGA-F012 and records pass, block, or waived state for every feature-oracle item.",
      "Agent-loop, skills, MCP, memory, RAG, context, UI, session, eval, and release gates remain linked in the continuity ledger.",
      "Existing open-source platform and general assistant release readiness reports remain visible as inherited context.",
      "Independent critic evidence confirms the terminal change uses a minimal-change scope."
    ],
    "compliance_gates": [
      "No secret values are printed, committed, or copied into reports.",
      "Release blockers name variable keys, failing commands, or missing services without credential values.",
      "Production deployment, package publishing, credential rotation, and production migrations require explicit approval.",
      "Safety gates cover prompt injection, tool boundary, SSRF/safe-fetch, tenant isolation, and failure isolation.",
      "Frontend release checks preserve auth protection and public route behavior."
    ],
    "acceptance_gates": [
      "NGA-F012 is passing or blocked with a named release/eval blocker.",
      "Every feature-oracle item has current evidence, blocker, or waiver status.",
      "Terminal whole-demand regression is recorded in the NGA-05 report.",
      "Rollback plan names the files, flags, commands, and release blockers needed to revert.",
      "Independent critic evidence and minimal-change scope notes appear in the NGA-05 report."
    ],
    "rollback_plan": [
      "Revert touched tests, docs, release notes, and frontend smoke files.",
      "Do not deploy or publish from this phase without explicit approval.",
      "If env validation blocks release, keep code deliverable status separate from release-ready status.",
      "If assistant evals fail, keep NGA-F012 blocked and leave dependent release actions locked."
    ]
  },
  "evidence": {
    "outputs": [
      "docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md",
      "web/test-results"
    ],
    "required_artifacts": [
      "phase report with validation output",
      "progress log entry",
      "feature oracle evidence for NGA-F012 and whole-demand regression",
      "continuity ledger code-summary writeback",
      "source packet code facts for eval, safety, and release gates",
      "handoff entry with terminal decision",
      "independent critic evidence and minimal-change scope notes",
      "browser evidence or precise runtime blocker",
      "whole-demand regression table"
    ],
    "waiver_policy": "A skipped release, env, browser, or eval gate requires explicit user waiver or a report blocker naming residual risk and release impact.",
    "next_phase_handoff": "This is the terminal gate. If blocked, the handoff must name the next concrete unblock action; if passed, the goal can be marked complete."
  },
  "stop_conditions": [
    "Stop if deployment or package publishing is required without explicit approval.",
    "Stop if secret values would need to be printed.",
    "Stop if production migrations or production data access are required.",
    "Stop if whole-demand regression cannot classify every oracle item."
  ]
}
```

## Coding Agent Contract

- PHASE_ID: NGA-05
- GOAL_TARGET: Run eval, safety, deployment, rollback, and whole-demand regression gates for the upgraded assistant.
- GOAL_PROMPT: Complete NGA-05 Evaluation Safety and Release Gate for `.` by following `docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md`; work on NGA-F012; run whole-demand regression across completed oracle items; stay inside the named edit boundaries; finish only after validation, regression, browser, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: NGA-04
- READ_FIRST: `docs/general_ai_assistant_next_gen/README.md`, `docs/general_ai_assistant_next_gen/phase-manifest.md`, this file
- PRIMARY_CONTEXT: assistant safety tests, assistant integration tests, frontend e2e smoke, `docker-compose.yml`, `.env.example`, `Makefile`, `README.md`, `RELEASE.md`, inherited GAA-04 release readiness report
- LIKELY_EDIT_PATHS: assistant safety/integration tests, frontend smoke specs, README/RELEASE notes, `docs/general_ai_assistant_next_gen/**`
- DO_NOT_EDIT: real env files, production deployment targets, package registry credentials, production migrations, git history
- EXECUTION_MODE: plan-first; execute terminal gates; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_eval_safety_contracts.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py tests/services/assistant/test_tool_orchestrator.py`; `uv run --extra dev --extra test pytest -q --no-cov tests/integration/test_assistant_openapi_contract.py tests/integration/test_assistant_core_isolation.py tests/integration/test_service_failure_isolation.py`; `pnpm -C web type-check && pnpm -C web lint && pnpm -C web build && pnpm -C web e2e:opensource`; `docker compose --env-file .env.example config --quiet`; `make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env`; `make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env`; `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`
- BROWSER_CHECKS: public route smoke, assistant browser smoke if UI changed, Playwright artifact path capture.
- REGRESSION_SCOPE: Whole-demand regression for NGA-F001 through NGA-F012; continuity ledger links agent-loop, skills, MCP, memory, RAG, context, UI, session, eval, and release.
- COMPLIANCE_GATES: No secrets; approval gates for deploy/publish/migrations; safety gates for prompt injection, tool boundary, SSRF, tenant isolation, and service failure.
- ROLLBACK_PLAN: Revert touched test/docs/smoke files, do not deploy or publish without approval, keep release blockers separate from code-delivery status.
- ACCEPTANCE_GATES: NGA-F012 has evidence or precise blocker; every oracle item is classified; whole-demand regression is recorded; independent critic evidence and minimal-change scope notes are recorded.
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md`
- STOP_CONDITIONS: Stop if deployment, publishing, secret printing, production migration, production data access, or unclassified oracle items are required.

## Task Spec

NGA-05 is the terminal proof phase. It decides whether the next-generation assistant upgrade is complete, blocked, waived, or ready for a controlled release. It must not confuse a pushed code state with a release-ready state.

## Problem Boundary

This phase may add or repair tests, smoke specs, release docs, and harness evidence. It must not perform deployment, publishing, production migration, or credential rotation without explicit approval.

## Context Policy

Read terminal test, frontend smoke, compose, release, and harness files. Do not read real env values into chat or reports. Report env blockers by variable name only.

## Requirements

### R1 Eval and Safety

Assistant eval, guardrail, safe-fetch, tool orchestration, prompt-injection, and failure-isolation gates are run or blocked with precise evidence.

### R2 Release Readiness

Compose config, frontend build, route smoke, env validation, runtime validation, rollback, and release notes are proven or blocked without exposing secrets.

### R3 Whole-Demand Regression

Every feature-oracle item NGA-F001 through NGA-F012 is classified with evidence, blocker, or waiver.

### R4 Terminal Handoff

The final handoff states whether the upgrade can ship, what remains blocked, and which rollback path applies.

## Test and Regression Requirements

Run assistant safety pytest, assistant integration pytest, frontend release checks, compose config validation, env validation, runtime validation, and strict harness validation. Record skips and blockers precisely.

## Compliance and Safety Requirements

No secret values in output. Deployment, publishing, credential rotation, production migrations, and production data access require explicit approval. Release blockers must not be hidden.

## Rollback and Recovery

Rollback is a focused revert of terminal changes plus disabling the upgraded assistant path through existing configuration if runtime gates fail. Release blockers keep release locked.

## Execution Capture

The report must include command evidence, browser artifact paths, whole-demand regression table, release decision, rollback notes, independent critic evidence, minimal-change scope, and final handoff.

## Evaluator Protocol

The independent critic checks whether the terminal report proves every explicit requirement, whether skipped gates are justified, whether release blockers are separate from code delivery, and whether no secret values leaked.

## Critic Protocol

The independent critic must review the actor report, changed files, validation output, browser/runtime evidence when required, feature-oracle evidence, and minimal-change scope from a fresh context. The critic artifact must include `Critic Verdict`, cite the actor report reviewed, and record any residual risk or waiver.

## Acceptance Criteria

- NGA-F012 has evidence or a precise blocker.
- Whole-demand regression classifies every oracle item.
- Required validation commands pass or record exact blockers.
- Independent critic evidence and minimal-change scope notes are present.
- Terminal decision is clear: passed, blocked, or waived.

## Risks

- Env validation may stay blocked by missing release variables.
- Live provider tests may be unavailable; mock/golden coverage must be explicit.
- Release workflows can fail if image names or service paths drift; terminal review must check those paths before publishing.
