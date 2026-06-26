# NGA-01 F003 Trace and Activity Records Report

**Phase:** NGA-01 Minimum Viable Agent Harness
**Feature-oracle item:** NGA-F003
**Status:** passing

## Scope

This loop item proves the existing streaming-first `AgentLoop` produces
inspectable trace and activity records for UI timelines, debugging, evals, and
release review. The patch stays inside the current backend harness and focused
assistant tests. It does not add a second loop, frontend work, schema changes,
deployments, migrations, or provider credential use.

## Implementation Summary

- Added focused tests for traceable run/tool/artifact records, approval pause
  records, and redacted run-error events.
- Added event-facing redaction for common secret-bearing text such as bearer
  auth headers and token/key/password assignments.
- Added common `run_id`, `thread_id`, and `session_id` fields to existing
  lifecycle, gateway, queue, sandbox, approval, context compaction, tool, and
  error events emitted by the streaming-first loop.
- Enriched `artifact_created` payloads at the loop boundary with
  `tool_call_id` and `tool_name`, so artifact activity can be tied to the tool
  call that created it.
- Preserved legacy event names such as `tool_call_started` and
  `tool_call_completed` while keeping the canonical `tool_call_start`,
  `tool_call_result`, and `tool_call_end` aliases from NGA-F002.

## Event Contract Summary

Minimum inspectable event schema now covered by focused tests:

| Event | Required trace fields |
| --- | --- |
| `run_started` | `run_id`, `thread_id`, `session_id`, `request_id`, `mode` |
| `gateway_decision` | `run_id`, `thread_id`, `session_id`, routing/policy profile fields |
| `context_budget` | `run_id`, `thread_id`, `session_id`, count-only context telemetry |
| `tool_call_start` | `run_id`, `thread_id`, `session_id`, `tool_call_id`, `name`, `step_id` |
| `tool_call_result` | `run_id`, `thread_id`, `session_id`, `tool_call_id`, `status`, redacted error/result preview |
| `tool_call_end` | `run_id`, `thread_id`, `session_id`, `tool_call_id`, `name`, `status`, duration, redacted error |
| `approval_required` | `run_id`, `thread_id`, `session_id`, `tool_id`, `tool_name`, `status: pending`, redacted reason |
| `context_compacted` | `run_id`, `thread_id`, `session_id`, token stats, trigger, reason |
| `artifact_created` | `run_id`, `thread_id`, `session_id`, `artifact_id`, `tool_call_id`, `tool_name` |
| `run_finished` | `run_id`, `thread_id`, usage/mode metadata |
| `run_error` | `run_id`, `thread_id`, redacted error or recoverable no-text reason |

`approval_result` is already part of the frozen stream-event vocabulary. This
backend-only loop item covers the server-owned pause state that the current
permission and gateway paths emit during a run; the UI/API approval round-trip
remains a downstream consumer of that event vocabulary.

## Validation Evidence

| Gate | Result | Evidence |
| --- | --- | --- |
| Red test | failed as intended | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py -k "trace_activity or approval_required_event or run_error_event"` failed 3 selected tests before production changes: missing `thread_id`, missing approval `run_id`, and raw secret-bearing `run_error`. |
| Green test | passed | Same command passed after the patch: 3 passed, 9 deselected, 1 Starlette deprecation warning. |
| Streaming-first focused file | passed | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py`: 12 passed, 1 Starlette deprecation warning. |
| Required focused pytest | passed | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py`: 67 passed, 1 Starlette deprecation warning. |
| Required broad ruff | blocked by existing lint debt | The required broad ruff command still reports 56 findings across pre-existing phase-scope files. This pass did not mass-fix unrelated lint. |
| Narrow ruff sanity | passed | `uv run ruff check tests/services/assistant/test_agentloop_streaming_first_contract.py`: all checks passed. |
| Undefined-name sanity | passed | `uv run ruff check --select F821,F823 apps/assistant-service/src/assistant_service/core/agent/agent_loop.py tests/services/assistant/test_agentloop_streaming_first_contract.py`: all checks passed. |
| Strict harness validation | passed | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`: quality score 100 after final evidence writeback. |
| NGA-01 completion gate | passed | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --completion-gate --phase NGA-01`: harness validation passed. |
| JSON checks | passed | `feature-oracle.json`, `loop-state.json`, and `loop-contract.json` parsed cleanly after final evidence writeback. |
| `git diff --check` | passed | No whitespace errors after final evidence writeback. |

## Minimal Change Scope

The implementation is additive on existing event payloads. It does not change
public function signatures, database schema, routing, deployment scripts,
frontend files, provider configuration, or production data. It keeps the
existing streaming-first `AgentLoop` as the only harness loop.

## Feature Oracle Updates

`docs/general_ai_assistant_next_gen/feature-oracle.json` updates only the active
`NGA-F003` item's allowed fields:

- `status`: `passing`
- `evidence`:
  `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f003-report.md`
  and
  `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f003-critic.md`
- `notes`: summarizes the new trace/activity correlation fields, event-facing
  redaction, and the documented broad-ruff caveat.

## Risks and Caveats

- Broad ruff remains blocked by pre-existing lint debt in the phase scope. The
  touched test file and fatal undefined-name checks are clean.
- Event-facing secret redaction covers common auth/key/token/password patterns;
  it is not a replacement for future structured secret classification in the
  evaluation/safety phase.

## Decision

`NGA-F003` is passing with actor and critic evidence. Because `NGA-F002` was
already passing with actor and critic evidence, NGA-01 is complete and the
completion gate passes.
