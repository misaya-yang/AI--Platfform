# Phase 03 - AI Evaluation and Safety

> For agentic workers: enter plan-first mode before editing. Execute this phase only, write evidence, and do not advance until acceptance gates pass or blockers are documented.

**Goal:** Add golden assistant behavior and safety evidence for the selected capability.

**Architecture:** Assistant behavior spans assistant-service runtime, gateway assistant routes, tool orchestration, guardrails, safe fetch, memory, working memory, model registry, and streaming event normalization.

**Tech Stack:** pytest, assistant-service test suite, mocked provider clients, tool orchestration tests, guardrail tests, safe fetch tests, golden agent loop tests.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "hybrid",
  "phase": {
    "id": "GAA-03",
    "number": "03",
    "title": "AI Evaluation and Safety",
    "status": "planned",
    "type": "eval",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_upgrade",
    "phase_file": "docs/general_ai_assistant_upgrade/phase-03-ai-evaluation-and-safety.md",
    "depends_on": ["GAA-02"],
    "unlocks": ["GAA-04"]
  },
  "goal": {
    "target": "Add golden assistant behavior and safety evidence for the selected capability.",
    "prompt": "Complete GAA-03 AI Evaluation and Safety for `.` by following `docs/general_ai_assistant_upgrade/phase-03-ai-evaluation-and-safety.md`; work on feature-oracle item GAA-F004; add deterministic assistant golden, tool-boundary, privacy, refusal, and cost/quota evidence; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_upgrade/reports/gaa-03-ai-evaluation-and-safety-plan.md",
    "completion_report": "docs/general_ai_assistant_upgrade/reports/gaa-03-ai-evaluation-and-safety-report.md"
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
    "read_first": ["docs/general_ai_assistant_upgrade/README.md", "docs/general_ai_assistant_upgrade/phase-manifest.md", "docs/general_ai_assistant_upgrade/loop-contract.json", "docs/general_ai_assistant_upgrade/loop-state.json", "docs/general_ai_assistant_upgrade/feature-oracle.json", "docs/general_ai_assistant_upgrade/progress-log.md", "docs/general_ai_assistant_upgrade/agent-handoff.md", "docs/general_ai_assistant_upgrade/continuity-ledger.md", "docs/general_ai_assistant_upgrade/next-window-prompt.md", "docs/general_ai_assistant_upgrade/source-packet.md", "docs/general_ai_assistant_upgrade/reports/gaa-02-assistant-user-experience-report.md", "docs/general_ai_assistant_upgrade/phase-03-ai-evaluation-and-safety.md"],
    "primary_context": ["tests/services/assistant/test_agent_loop_golden.py", "tests/services/assistant/test_guardrails.py", "tests/services/assistant/test_safe_fetch.py", "tests/services/assistant/test_tool_orchestrator.py", "tests/services/assistant/test_memory_manager.py", "tests/services/assistant/test_working_memory.py", "apps/assistant-service/src/assistant_service", "web/src/pages/assistant/sse-events.ts"],
    "context_budget": "focused",
    "do_not_load_unless": ["live provider credentials", "production traces", "user conversation exports", "provider dashboards"]
  },
  "boundaries": {
    "likely_edit_paths": ["tests/services/assistant/**", "apps/assistant-service/src/assistant_service/**", "web/src/pages/assistant/sse-events.ts", "docs/general_ai_assistant_upgrade/**"],
    "do_not_edit": [".env", "database/migrations/**", "docker-compose.yml", "production systems", "provider dashboards", "billing code outside quota evidence"],
    "external_inputs": ["mock provider responses", "golden prompt fixtures", "local trace logs"],
    "secrets_required": []
  },
  "tool_policy": {
    "allowed_tools": ["repo search", "shell validation", "apply_patch", "code review", "code simplifier"],
    "approval_required": ["live provider evaluation", "new dependency", "deployment", "production data mutation", "external provider change"],
    "dangerous_commands": ["git reset --hard", "rm -rf", "docker compose down -v", "DROP SCHEMA", "TRUNCATE"]
  },
  "risk": {
    "tags": ["ai", "agent", "llm", "eval", "security"],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": false,
    "ai_eval_required": true,
    "external_service_required": false,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {"id": "agent-loop-golden", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agent_loop_golden.py", "expected": "command exits 0", "required": true},
      {"id": "assistant-safety-targets", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_tool_orchestrator.py", "expected": "command exits 0", "required": true},
      {"id": "assistant-memory-targets", "cwd": ".", "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py", "expected": "command exits 0", "required": true},
      {"id": "assistant-eval-ruff", "cwd": ".", "command": "uv run ruff check apps/assistant-service/src/assistant_service tests/services/assistant", "expected": "command exits 0 for touched files or report lists pre-existing lint blockers", "required": true}
    ],
    "browser_checks": ["No browser route is changed in this phase; UI evidence is inherited from GAA-02 report."],
    "regression_scope": ["golden assistant responses", "tool source boundaries", "safe fetch allow/deny behavior", "guardrail refusal behavior", "memory privacy", "working memory lifecycle", "quota or cost accounting evidence"],
    "compliance_gates": ["no live provider call without approval", "no user PII in fixtures", "unsafe URLs remain blocked", "tool output cannot override system instructions", "refusal and privacy behavior are covered"],
    "acceptance_gates": ["GAA-F004 status has eval evidence", "golden prompts and expected outcomes are documented", "mock provider path is deterministic", "privacy and refusal gates are represented", "continuity ledger records eval criteria for release", "review evidence is recorded", "minimal-change scope is documented"],
    "rollback_plan": ["revert eval fixtures and assistant-service changes touched in this phase", "restore previous golden expectations if implementation is reverted", "leave failed eval details in report"]
  },
  "evidence": {
    "outputs": ["docs/general_ai_assistant_upgrade/reports/gaa-03-ai-evaluation-and-safety-report.md"],
    "required_artifacts": ["phase report", "pytest output summary", "golden prompt table", "safety gate summary", "progress-log entry", "feature-oracle evidence", "continuity-ledger update", "source-packet update", "handoff update"],
    "waiver_policy": "A waived AI behavior gate requires user waiver, reason, and residual release risk.",
    "next_phase_handoff": "Unlock GAA-04 only after eval and safety criteria are release-ready."
  },
  "stop_conditions": ["GAA-02 report is missing", "deterministic mock path cannot be built", "live provider credentials are required", "PII fixture would be needed", "safety gate cannot be expressed as a test"]
}
```

## Coding Agent Contract

- PHASE_ID: GAA-03
- GOAL_TARGET: Add golden assistant behavior and safety evidence for the selected capability.
- GOAL_PROMPT: Complete GAA-03 AI Evaluation and Safety for `.` by following `docs/general_ai_assistant_upgrade/phase-03-ai-evaluation-and-safety.md`; work on feature-oracle item GAA-F004; add deterministic assistant golden, tool-boundary, privacy, refusal, and cost/quota evidence; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: GAA-02
- READ_FIRST: `docs/general_ai_assistant_upgrade/README.md`, `docs/general_ai_assistant_upgrade/phase-manifest.md`, `reports/gaa-02-assistant-user-experience-report.md`, this file
- PRIMARY_CONTEXT: `tests/services/assistant/test_agent_loop_golden.py`, `tests/services/assistant/test_guardrails.py`, `tests/services/assistant/test_safe_fetch.py`, `tests/services/assistant/test_tool_orchestrator.py`, `tests/services/assistant/test_memory_manager.py`, `tests/services/assistant/test_working_memory.py`, `apps/assistant-service/src/assistant_service`, `web/src/pages/assistant/sse-events.ts`
- LIKELY_EDIT_PATHS: `tests/services/assistant/**`, `apps/assistant-service/src/assistant_service/**`, `web/src/pages/assistant/sse-events.ts`, `docs/general_ai_assistant_upgrade/**`
- DO_NOT_EDIT: `.env`, `database/migrations/**`, `docker-compose.yml`, production systems, provider dashboards, billing code outside quota evidence
- EXECUTION_MODE: plan-first; implement stepwise; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agent_loop_golden.py`; assistant safety pytest targets; assistant memory pytest targets; ruff for touched assistant files
- BROWSER_CHECKS: no browser route changed in this phase; UI evidence inherited from GAA-02 report
- REGRESSION_SCOPE: golden responses, tool boundaries, safe fetch, guardrails, memory privacy, working memory, quota or cost evidence
- COMPLIANCE_GATES: no live provider call without approval; no PII fixtures; unsafe URLs blocked; tool output cannot override system instructions; refusal and privacy covered
- ROLLBACK_PLAN: revert eval fixtures and assistant-service changes touched in this phase; restore previous golden expectations if implementation is reverted
- ACCEPTANCE_GATES: GAA-F004 has eval evidence; golden prompt table documented; deterministic mock path exists; privacy and refusal gates represented; ledger records release criteria; review evidence recorded; minimal-change scope documented
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_upgrade/reports/gaa-03-ai-evaluation-and-safety-report.md`
- STOP_CONDITIONS: GAA-02 missing, deterministic mock path unavailable, live provider credentials required, PII fixture required, safety gate cannot be tested

## Task Spec

Add evaluation coverage for one assistant capability selected from GAA-02 handoff and prove safety behavior through deterministic tests.

## Problem Boundary

This phase does not ship UI changes or execute live provider evaluations without approval.

## Context Policy

Use local fixtures and mocks. Do not read private conversation exports or provider dashboards.

## Requirements

### R1 Golden Behavior

The selected assistant capability must have deterministic prompts, expected outcomes, and failure-mode tests.

### R2 Safety Boundary

Tool output, web fetches, memory, and refusal behavior must keep source and privacy boundaries intact.

### R3 Release Handoff

Release phase must receive a clear eval table and residual risk list.

## Test and Regression Requirements

Run the eval, guardrail, safe-fetch, tool, memory, and ruff commands listed above.

## Compliance and Safety Requirements

No PII fixture, no live provider call without approval, no unsafe URL access, and no secret exposure.

## Rollback and Recovery

Revert eval and assistant-service changes touched in this phase if deterministic checks fail.

## Execution Capture

Write `reports/gaa-03-ai-evaluation-and-safety-report.md` with golden question table, command summaries, and safety findings.

## Evaluator Protocol

Independently inspect eval assertions and confirm they would fail for unsafe tool, privacy, or refusal behavior.

## Acceptance Criteria

- Required eval commands pass.
- GAA-F004 has evidence.
- GAA-04 receives release-ready eval gates and residual risks.

## Risks

- Model behavior can be nondeterministic; this phase requires mocked provider paths for reliable release evidence.
