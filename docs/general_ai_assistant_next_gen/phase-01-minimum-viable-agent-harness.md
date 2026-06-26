# Phase 01 - Minimum Viable Agent Harness

> For agentic workers: enter plan-first mode before editing. Execute this phase only, work on NGA-F002 and NGA-F003, and write evidence before handoff.

**Goal:** Define and implement the smallest canonical assistant harness contract that makes every run observable, policy-bound, streaming-first, and testable.

**Architecture:** NGA-01 sits inside assistant-service and its gateway execution path. It should clarify the existing streaming-first `AgentLoop`, gateway policy routing, middleware chain, tool orchestration, context-budget events, tool lifecycle events, run finish/error events, and trace persistence without adding a heavy planner-by-default stack.

**Tech Stack:** Python 3.12, FastAPI assistant-service, `AgentLoop`, `AssistantExecutionGateway`, middleware chain, tool invoker, tool orchestrator, pytest, ruff, and harness validator.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "execution",
  "phase": {
    "id": "NGA-01",
    "number": "01",
    "title": "Minimum Viable Agent Harness",
    "status": "planned",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_next_gen",
    "phase_file": "docs/general_ai_assistant_next_gen/phase-01-minimum-viable-agent-harness.md",
    "depends_on": [
      "NGA-00"
    ],
    "unlocks": [
      "NGA-02"
    ]
  },
  "goal": {
    "target": "Make the assistant run loop a single streaming-first contract with explicit lifecycle, policy, tool, memory, context, and trace evidence.",
    "prompt": "Complete NGA-01 Minimum Viable Agent Harness for `.` by following `docs/general_ai_assistant_next_gen/phase-01-minimum-viable-agent-harness.md`; work on NGA-F002 and NGA-F003; keep changes inside the named assistant-service and test boundaries; finish only after validation, regression, independent critic evidence, minimal-change scope, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-plan.md",
    "completion_report": "docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-report.md"
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
      "docs/general_ai_assistant_next_gen/phase-01-minimum-viable-agent-harness.md"
    ],
    "primary_context": [
      "apps/assistant-service/src/assistant_service/core/agent/agent_loop.py",
      "apps/assistant-service/src/assistant_service/core/agent/middleware.py",
      "apps/assistant-service/src/assistant_service/core/agent/middlewares/permission.py",
      "apps/assistant-service/src/assistant_service/core/agent/middlewares/response_cap.py"
    ],
    "context_budget": "focused",
    "do_not_load_unless": [
      "web UI files",
      "database migrations",
      "provider dashboards",
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
      "apps/assistant-service/src/assistant_service/core/agent/**",
      "apps/assistant-service/src/assistant_service/core/gateway/**",
      "apps/assistant-service/src/assistant_service/core/tool_invoker.py",
      "apps/assistant-service/src/assistant_service/core/tool_orchestrator.py",
      "apps/assistant-service/src/assistant_service/core/tools/tool_selector.py",
      "tests/services/assistant/test_agentloop_streaming_first_contract.py",
      "tests/services/assistant/test_agent_loop_golden.py",
      "tests/services/assistant/test_middleware_chain.py",
      "tests/services/assistant/test_tool_orchestrator.py",
      "docs/general_ai_assistant_next_gen/**"
    ],
    "do_not_edit": [
      "database/**",
      "web/**",
      "deployment scripts",
      "provider credentials",
      "MCP tenant policy defaults outside documented scope"
    ],
    "external_inputs": [
      "No live provider credential is required; use fake model/tool fixtures."
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
      "harness validator"
    ],
    "approval_required": [
      "schema migrations",
      "deployment",
      "provider credential use",
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
      "security"
    ],
    "data_mutation": false,
    "migration_required": false,
    "browser_required": false,
    "ai_eval_required": true,
    "external_service_required": false,
    "release_blocking": false
  },
  "validation": {
    "commands": [
      {
        "id": "agent-harness-pytest",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py",
        "expected": "Agent loop lifecycle, golden streaming behavior, middleware ordering, and tool orchestration tests pass.",
        "required": true
      },
      {
        "id": "agent-harness-ruff",
        "cwd": ".",
        "command": "uv run ruff check apps/assistant-service/src/assistant_service/core/agent apps/assistant-service/src/assistant_service/core/gateway apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/tool_orchestrator.py apps/assistant-service/src/assistant_service/core/tools/tool_selector.py tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py",
        "expected": "Touched harness, gateway, tool orchestration, and focused tests pass ruff.",
        "required": true
      },
      {
        "id": "strict-harness-validation",
        "cwd": ".",
        "command": "python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score",
        "expected": "Harness remains strict-validator clean after NGA-01 evidence writeback.",
        "required": true
      }
    ],
    "browser_checks": [
      "No browser route is required for NGA-01 because the phase targets backend run-loop contracts."
    ],
    "regression_scope": [
      "Existing chat streaming still emits token/content/tool/artifact events in the order expected by golden tests.",
      "Permission and response-cap middleware behavior remains compatible with existing tests.",
      "Tool orchestration still supports dependency ordering, caching, failure records, and working-memory updates.",
      "Independent critic evidence confirms the implementation uses a minimal-change scope and avoids a new planner-by-default abstraction."
    ],
    "compliance_gates": [
      "Lifecycle and trace events must redact secrets, provider keys, signed URLs, request auth headers, and raw credentials.",
      "Policy decisions must preserve tenant, user, session, run, KB, and permission metadata.",
      "Tool invocation failures must be observable without exposing secret-bearing payloads.",
      "AI and agent behavior must be tested through fake model/tool fixtures or golden traces."
    ],
    "acceptance_gates": [
      "NGA-F002 is passing with command and report evidence.",
      "NGA-F003 is passing or explicitly blocked with a named trace/event gap.",
      "The canonical event set covers run_started, gateway_decision, context_budget or context_compacted, tool start/end, approval states, artifact creation, run_finished, and run_error.",
      "Independent critic evidence and minimal-change scope notes appear in the NGA-01 report."
    ],
    "rollback_plan": [
      "Revert changed assistant-service run-loop, gateway, tool, and focused test files.",
      "Restore NGA-F002 and NGA-F003 statuses to failing or blocked if validation cannot be recovered."
    ]
  },
  "evidence": {
    "outputs": [
      "docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-report.md"
    ],
    "required_artifacts": [
      "phase report with validation output",
      "progress log entry",
      "feature oracle evidence for NGA-F002 and NGA-F003",
      "continuity ledger code-summary writeback",
      "source packet code facts for the canonical harness contract",
      "handoff entry for NGA-02",
      "independent critic evidence and minimal-change scope notes"
    ],
    "waiver_policy": "A skipped agent-loop gate requires a report blocker naming the missing fixture, command, or code boundary.",
    "next_phase_handoff": "NGA-02 may start only after the event and harness contract is stable enough for skills and MCP to attach to it."
  },
  "stop_conditions": [
    "Stop if the phase requires database schema changes.",
    "Stop if a live provider key is required for proof.",
    "Stop if the implementation would add a second competing agent loop.",
    "Stop if UI changes are required before the backend contract is stable."
  ]
}
```

## Coding Agent Contract

- PHASE_ID: NGA-01
- GOAL_TARGET: Make the assistant run loop a single streaming-first contract with explicit lifecycle, policy, tool, memory, context, and trace evidence.
- GOAL_PROMPT: Complete NGA-01 Minimum Viable Agent Harness for `.` by following `docs/general_ai_assistant_next_gen/phase-01-minimum-viable-agent-harness.md`; work on NGA-F002 and NGA-F003; stay inside the named edit boundaries; finish only after validation, regression, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: NGA-00
- READ_FIRST: `docs/general_ai_assistant_next_gen/README.md`, `docs/general_ai_assistant_next_gen/phase-manifest.md`, this file
- PRIMARY_CONTEXT: `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py`, `apps/assistant-service/src/assistant_service/core/gateway/execution_gateway.py`, `apps/assistant-service/src/assistant_service/core/gateway/policy_engine.py`, `apps/assistant-service/src/assistant_service/core/tool_invoker.py`, `apps/assistant-service/src/assistant_service/core/tool_orchestrator.py`, `apps/assistant-service/src/assistant_service/core/tools/tool_selector.py`, `tests/services/assistant/test_agentloop_streaming_first_contract.py`, `tests/services/assistant/test_agent_loop_golden.py`, `tests/services/assistant/test_middleware_chain.py`, `tests/services/assistant/test_tool_orchestrator.py`
- LIKELY_EDIT_PATHS: `apps/assistant-service/src/assistant_service/core/agent/**`, `apps/assistant-service/src/assistant_service/core/gateway/**`, `apps/assistant-service/src/assistant_service/core/tool_invoker.py`, `apps/assistant-service/src/assistant_service/core/tool_orchestrator.py`, `apps/assistant-service/src/assistant_service/core/tools/tool_selector.py`, focused assistant tests, `docs/general_ai_assistant_next_gen/**`
- DO_NOT_EDIT: database migrations, frontend files, deployment scripts, env files, production data, unrelated provider adapters
- EXECUTION_MODE: plan-first; implement one harness slice; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py`; `uv run ruff check apps/assistant-service/src/assistant_service/core/agent apps/assistant-service/src/assistant_service/core/gateway apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/tool_orchestrator.py apps/assistant-service/src/assistant_service/core/tools/tool_selector.py tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py`; `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`
- BROWSER_CHECKS: No browser route is required for this backend contract phase.
- REGRESSION_SCOPE: Chat streaming order, middleware behavior, tool orchestration, and trace redaction remain compatible with focused tests.
- COMPLIANCE_GATES: Redact secrets; preserve tenant and session boundaries; use fake model/tool fixtures; reject raw credential traces.
- ROLLBACK_PLAN: Revert touched backend/test files and restore NGA-F002/NGA-F003 evidence to failing or blocked.
- ACCEPTANCE_GATES: NGA-F002 passing; NGA-F003 passing or blocked with a named event gap; canonical event set is documented and tested; independent critic evidence and minimal-change scope notes are recorded.
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-report.md`
- STOP_CONDITIONS: Stop if schema changes, live provider credentials, a second agent loop, or UI changes are required.

## Task Spec

NGA-01 turns the assistant runtime into a single durable harness contract: model plus harness, not a collection of competing flows. The intended result is a small run-state/event protocol that later skills, MCP, memory, RAG, UI, and eval phases can depend on.

## Problem Boundary

This phase may clarify or patch the existing `AgentLoop`, gateway policy path, middleware, tool invoker, tool orchestrator, and tests. It must not build the full skills catalog, memory taxonomy, UI control surface, or release harness.

## Context Policy

Read only the named backend files and focused tests before planning. Load frontend, database, and deployment files only after writing a blocker that explains why the backend run contract cannot be completed without them.

## Requirements

### R1 Canonical Run Contract

Every run has a consistent lifecycle with run start, gateway decision, context budget or compaction evidence, tool lifecycle, approval state, artifact creation, finish, and error events.

### R2 Streaming-First Behavior

The default path remains streaming-first. Planning is allowed as a bounded mode, not as a mandatory heavy loop for every request.

### R3 Policy and Tool Boundaries

Gateway policy, middleware, tenant context, tool selection, and tool invocation metadata are preserved across every tool call and failure path.

### R4 Trace and Debug Evidence

Events and traces are inspectable for debugging and evals without leaking secrets.

## Test and Regression Requirements

Run the focused pytest command, focused ruff command, and strict harness validator. Add or adjust tests for any newly defined event or state contract.

## Compliance and Safety Requirements

Never log provider keys, auth headers, signed URLs, raw credentials, or private document content. Keep tenant/user/session/run metadata attached to tool and memory events.

## Rollback and Recovery

Rollback is a focused revert of touched assistant-service and test files. If the event contract is only partially proven, mark the affected oracle item blocked and keep NGA-02 locked.

## Execution Capture

The phase report must include command output summaries, changed file list, event contract summary, independent critic notes, minimal-change scope, and downstream handoff facts.

## Evaluator Protocol

The independent critic checks that a second agent loop was not introduced, that old streaming behavior is preserved, that tests cover success and failure paths, and that the report records independent critic evidence.

## Critic Protocol

The independent critic must review the actor report, changed files, validation output, browser/runtime evidence when required, feature-oracle evidence, and minimal-change scope from a fresh context. The critic artifact must include `Critic Verdict`, cite the actor report reviewed, and record any residual risk or waiver.

## Acceptance Criteria

- Focused backend tests pass.
- Focused ruff passes or the report names pre-existing out-of-scope failures.
- NGA-F002 has evidence and is passing.
- NGA-F003 is passing or blocked with a precise event gap.
- NGA-02 has enough handoff detail to attach skills and MCP to the canonical run contract.

## Risks

- A broad rewrite would destabilize the assistant. Keep the change narrow.
- Event schemas can become noisy. Keep only state that supports UI timelines, debugging, evals, and release review.
