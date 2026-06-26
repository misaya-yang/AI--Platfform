# Phase 03 - Memory RAG and Context Foundation

> For agentic workers: execute this phase only after NGA-02 passes or is explicitly waived in its report. Work on NGA-F007, NGA-F008, and NGA-F009.

**Goal:** Establish explicit procedural, situational, and semantic memory boundaries; session-aware RAG; and context assembly that stays useful without bloating every turn.

**Architecture:** NGA-03 consumes the canonical run events and capability outputs from NGA-01 and NGA-02. It focuses on `MemoryManager`, `MemoryService`, runtime memory v2, memory tools, preference extraction, RAG context engine, query intent analysis, scenario-aware retrieval, file/session KB handling, compressor, and context budgeting.

**Tech Stack:** assistant-service memory/RAG Python modules, knowledge-service proxy boundaries, file processor, pytest, ruff, fake storage/retrieval fixtures, and harness validator.

---

## Machine Contract

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "execution",
  "phase": {
    "id": "NGA-03",
    "number": "03",
    "title": "Memory RAG and Context Foundation",
    "status": "planned",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/general_ai_assistant_next_gen",
    "phase_file": "docs/general_ai_assistant_next_gen/phase-03-memory-rag-and-context-foundation.md",
    "depends_on": [
      "NGA-02"
    ],
    "unlocks": [
      "NGA-04"
    ]
  },
  "goal": {
    "target": "Make memory, RAG, and context assembly explicit, scoped, privacy-aware, and measurable.",
    "prompt": "Complete NGA-03 Memory RAG and Context Foundation for `.` by following `docs/general_ai_assistant_next_gen/phase-03-memory-rag-and-context-foundation.md`; work on NGA-F007, NGA-F008, and NGA-F009; stay inside the named memory, RAG, file-processing, focused test, and harness boundaries; finish only after validation, regression, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-plan.md",
    "completion_report": "docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-report.md"
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
      "docs/general_ai_assistant_next_gen/phase-03-memory-rag-and-context-foundation.md"
    ],
    "primary_context": [
      "apps/assistant-service/src/assistant_service/core/memory/memory_manager.py",
      "apps/assistant-service/src/assistant_service/core/memory/compressor.py",
      "apps/assistant-service/src/assistant_service/core/memory/preference_extractor.py",
      "apps/assistant-service/src/assistant_service/core/memory_service.py"
    ],
    "context_budget": "focused",
    "do_not_load_unless": [
      "frontend UI files",
      "live knowledge-base data",
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
      "apps/assistant-service/src/assistant_service/core/memory/**",
      "apps/assistant-service/src/assistant_service/core/runtime/memory/**",
      "apps/assistant-service/src/assistant_service/core/runtime/context/**",
      "apps/assistant-service/src/assistant_service/core/rag/**",
      "apps/assistant-service/src/assistant_service/core/files/file_processor.py",
      "apps/assistant-service/src/assistant_service/core/tools/memory_tool.py",
      "tests/services/assistant/test_memory_manager.py",
      "tests/services/assistant/test_working_memory.py",
      "tests/services/assistant/test_compressor.py",
      "tests/services/assistant/test_context_engine.py",
      "tests/services/assistant/test_preference_extractor.py",
      "tests/services/assistant/test_guardrails.py",
      "tests/services/assistant/test_safe_fetch.py",
      "docs/general_ai_assistant_next_gen/**"
    ],
    "do_not_edit": [
      "database migrations without a migration plan and user approval",
      "knowledge-service ingestion internals unless a session KB blocker is recorded",
      "frontend files",
      "provider credentials",
      "production knowledge datasets"
    ],
    "external_inputs": [
      "Use fake memory stores, fake retrieval, and local fixtures for validation."
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
      "schema migration",
      "production data access",
      "live KB ingestion",
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
      "security",
      "database"
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
        "id": "memory-rag-context-pytest",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py",
        "expected": "Memory manager, working memory, compressor, context engine, and preference extraction tests pass.",
        "required": true
      },
      {
        "id": "memory-safety-pytest",
        "cwd": ".",
        "command": "uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py",
        "expected": "Guardrail and safe-fetch tests pass for memory/RAG privacy and external-source boundaries.",
        "required": true
      },
      {
        "id": "memory-rag-ruff",
        "cwd": ".",
        "command": "uv run ruff check apps/assistant-service/src/assistant_service/core/memory apps/assistant-service/src/assistant_service/core/runtime/memory apps/assistant-service/src/assistant_service/core/runtime/context apps/assistant-service/src/assistant_service/core/rag apps/assistant-service/src/assistant_service/core/files/file_processor.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py",
        "expected": "Touched memory, runtime context, RAG, file-processing, memory tool, and focused tests pass ruff.",
        "required": true
      },
      {
        "id": "strict-harness-validation",
        "cwd": ".",
        "command": "python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score",
        "expected": "Harness remains strict-validator clean after NGA-03 evidence writeback.",
        "required": true
      }
    ],
    "browser_checks": [
      "No browser route is required for NGA-03 because this phase targets backend memory, retrieval, and context contracts."
    ],
    "regression_scope": [
      "Existing memory modes and working-memory updates remain compatible with agent loop tests.",
      "Context budget events remain compatible with NGA-01 event contract.",
      "Skill and MCP outputs from NGA-02 are treated as untrusted sources in context assembly.",
      "Session file handling does not leak across tenant, user, or session boundaries.",
      "Independent critic evidence confirms the phase uses a minimal-change scope."
    ],
    "compliance_gates": [
      "Procedural, situational, and semantic memory storage locations are documented.",
      "Memory retrieval respects tenant, user, session, privacy, and delete boundaries.",
      "PII and prompt-injection filtering behavior is tested or blocked with a named gap.",
      "RAG citations and source summaries identify KB, session file, generated artifact, web, or MCP origin.",
      "Database or schema changes require explicit user approval and rollback notes before implementation."
    ],
    "acceptance_gates": [
      "NGA-F007 is passing or blocked with a named memory-boundary gap.",
      "NGA-F008 is passing or blocked with a named session-KB or citation gap.",
      "NGA-F009 is passing or blocked with a named context-budget gap.",
      "The report records a memory profile decision for off, basic, and hybrid behavior.",
      "Independent critic evidence and minimal-change scope notes appear in the NGA-03 report."
    ],
    "rollback_plan": [
      "Revert touched memory, RAG, file-processing, memory tool, and focused test files.",
      "Disable new memory or session-KB behavior behind existing configuration if a runtime gate fails.",
      "If a migration is proposed, stop before applying it and record a migration rollback design first."
    ]
  },
  "evidence": {
    "outputs": [
      "docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-report.md"
    ],
    "required_artifacts": [
      "phase report with validation output",
      "progress log entry",
      "feature oracle evidence for NGA-F007, NGA-F008, and NGA-F009",
      "continuity ledger code-summary writeback",
      "source packet code facts for memory, RAG, and context boundaries",
      "handoff entry for NGA-04",
      "independent critic evidence and minimal-change scope notes"
    ],
    "waiver_policy": "A skipped memory, RAG, or context gate must name the missing fixture, data boundary, or migration decision.",
    "next_phase_handoff": "NGA-04 may start only after memory, RAG, and context state are visible enough for the assistant UI to expose controls and recovery."
  },
  "stop_conditions": [
    "Stop if a schema migration is required without user approval.",
    "Stop if live private KB data is required for proof.",
    "Stop if memory deletion semantics cannot be proven or blocked explicitly.",
    "Stop if context assembly requires changing NGA-01 or NGA-02 contracts without dependency writeback."
  ]
}
```

## Coding Agent Contract

- PHASE_ID: NGA-03
- GOAL_TARGET: Make memory, RAG, and context assembly explicit, scoped, privacy-aware, and measurable.
- GOAL_PROMPT: Complete NGA-03 Memory RAG and Context Foundation for `.` by following `docs/general_ai_assistant_next_gen/phase-03-memory-rag-and-context-foundation.md`; work on NGA-F007, NGA-F008, and NGA-F009; stay inside the named edit boundaries; finish only after validation, regression, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: NGA-02
- READ_FIRST: `docs/general_ai_assistant_next_gen/README.md`, `docs/general_ai_assistant_next_gen/phase-manifest.md`, this file
- PRIMARY_CONTEXT: memory manager, memory service, runtime memory, runtime context, RAG context engine, file processor, memory tool, focused assistant memory/context/safety tests
- LIKELY_EDIT_PATHS: `apps/assistant-service/src/assistant_service/core/memory/**`, `apps/assistant-service/src/assistant_service/core/runtime/memory/**`, `apps/assistant-service/src/assistant_service/core/runtime/context/**`, `apps/assistant-service/src/assistant_service/core/rag/**`, `apps/assistant-service/src/assistant_service/core/files/file_processor.py`, `apps/assistant-service/src/assistant_service/core/tools/memory_tool.py`, focused assistant tests, `docs/general_ai_assistant_next_gen/**`
- DO_NOT_EDIT: database migrations without approval, frontend files, production KB data, env files, unrelated knowledge-service ingestion internals
- EXECUTION_MODE: plan-first; implement one foundation slice; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py`; `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py`; `uv run ruff check apps/assistant-service/src/assistant_service/core/memory apps/assistant-service/src/assistant_service/core/runtime/memory apps/assistant-service/src/assistant_service/core/runtime/context apps/assistant-service/src/assistant_service/core/rag apps/assistant-service/src/assistant_service/core/files/file_processor.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py`; `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`
- BROWSER_CHECKS: No browser route is required for this backend foundation phase.
- REGRESSION_SCOPE: Existing memory modes, context budget events, skill/MCP source treatment, session file boundaries, and fake retrieval behavior.
- COMPLIANCE_GATES: Tenant and privacy boundaries; PII filtering; source citations; memory delete behavior; migration approval gate.
- ROLLBACK_PLAN: Revert touched memory/RAG/context files, disable new memory/session-KB behavior through existing config, and block migrations until approved.
- ACCEPTANCE_GATES: NGA-F007, NGA-F008, and NGA-F009 have evidence or precise blockers; memory profiles are recorded; independent critic evidence and minimal-change scope notes are recorded.
- EVIDENCE_OUTPUT: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-report.md`
- STOP_CONDITIONS: Stop if unapproved schema migration, private live KB data, unproven delete behavior, or upstream contract changes are required.

## Task Spec

NGA-03 turns memory and context from implicit prompt stuffing into a typed assistant foundation. The key distinction is procedural memory for reviewed skills/workflows, situational memory for current sessions and artifacts, and semantic memory for retrievable long-term knowledge.

## Problem Boundary

This phase does not build the full UI controls for memory or release evals. It creates the backend contracts and tests that make those later surfaces truthful.

## Context Policy

Read only the named memory, RAG, file, tool, and focused test files. Do not load private KB data, env files, or production logs.

## Requirements

### R1 Memory Taxonomy

Procedural, situational, and semantic memory have explicit storage, retrieval, retention, inspect, delete, and privacy boundaries.

### R2 Session-Aware RAG

RAG can account for KB content, session files, generated artifacts, web/MCP outputs, and citations through scoped retrieval and source-aware context budgets.

### R3 Context Budgeting

Context assembly preserves stable prefixes, selected skills/tools, memory snippets, retrieval summaries, compaction events, and current user intent without injecting every available source.

### R4 Safety and Deletion

Memory and retrieval behavior respects tenant, user, session, PII, prompt-injection, and explicit deletion boundaries.

## Test and Regression Requirements

Run focused memory/context pytest, guardrail/safe-fetch pytest, focused ruff, and strict harness validation. Add tests for any changed memory profile, session KB, citation, or compaction behavior.

## Compliance and Safety Requirements

No private KB data is required. All external or retrieved content is treated as untrusted. Memory deletion and privacy claims require tests or a named blocker.

## Rollback and Recovery

Rollback is a focused revert. New memory/session-KB behavior must be disabled through existing config if a runtime gate fails.

## Execution Capture

The report must include memory taxonomy decisions, command evidence, independent critic notes, minimal-change scope, source-packet code facts, and UI handoff notes.

## Evaluator Protocol

The independent critic checks that memory scope is explicit, context budgets are measurable, citations identify source categories, and delete/privacy behavior is proven or blocked.

## Critic Protocol

The independent critic must review the actor report, changed files, validation output, browser/runtime evidence when required, feature-oracle evidence, and minimal-change scope from a fresh context. The critic artifact must include `Critic Verdict`, cite the actor report reviewed, and record any residual risk or waiver.

## Acceptance Criteria

- Focused memory/context tests pass.
- Focused safety tests pass.
- Focused ruff passes or out-of-scope lint failures are named.
- NGA-F007 through NGA-F009 have evidence or precise blockers.
- NGA-04 receives enough handoff detail to expose memory/context state honestly.

## Risks

- Memory can leak private data if tenant and delete boundaries are weak.
- RAG can over-retrieve and bloat context.
- Session KB support may require schema or knowledge-service changes; those need a separate approval gate.
