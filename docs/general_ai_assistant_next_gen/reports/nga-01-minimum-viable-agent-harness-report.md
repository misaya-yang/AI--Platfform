# NGA-01 Minimum Viable Agent Harness Phase Report

Status: passed with documented ruff caveat

**Phase:** NGA-01 Minimum Viable Agent Harness
**Completed features:** NGA-F002, NGA-F003
**Next phase unlocked:** NGA-02 Skills and MCP Capability Layer

## Scope

NGA-01 made the assistant runtime a single streaming-first harness contract with
explicit lifecycle, policy, tool, context, approval, artifact, finish/error, and
trace evidence. The implementation stayed inside assistant-service agent-loop
and focused test boundaries. No frontend, database, deployment, migration,
provider credential, or production data path was touched.

## Plan Followed

The durable plan is recorded at
`docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-plan.md`.
F002 and F003 were executed as separate one-feature loop items.

## Files Changed

- `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py`
- `tests/services/assistant/test_agentloop_streaming_first_contract.py`
- `docs/general_ai_assistant_next_gen/**`

## Feature Evidence

| Feature | Status | Evidence |
| --- | --- | --- |
| NGA-F002 | passing | Actor report: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f002-report.md`; critic artifact: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f002-critic.md`. |
| NGA-F003 | passing | Actor report: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f003-report.md`; critic artifact: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f003-critic.md`. |

## Event Contract Summary

- `run_started`, `gateway_decision`, `context_budget`, `tool_call_start`,
  `tool_call_result`, `tool_call_end`, `approval_required`,
  `context_compacted`, `artifact_created`, `run_finished`, and `run_error`
  now have inspectable run/session correlation in the streaming-first path.
- `artifact_created` events include the creating `tool_call_id` and
  `tool_name`.
- Event-facing message previews, tool arguments, policy reasons, result
  previews, and error payloads redact common auth/key/token/password patterns.
- Legacy tool events are preserved; canonical aliases remain additive.
- `approval_result` remains part of the frozen event vocabulary for downstream
  UI/API approval round-trip work. NGA-01 proves the backend run-loop pause
  state through `approval_required`.

## Validation Evidence

| Gate | Result | Evidence |
| --- | --- | --- |
| F002 red check | failed as intended | Missing `context_budget` and `tool_call_start` assertions failed before F002 production code changed. |
| F002 green check | passed | F002 selected streaming-first tests passed after patch. |
| F003 red check | failed as intended | F003 selected tests failed before production changes: missing common trace IDs and raw secret-bearing `run_error`. |
| F003 green check | passed | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py -k "trace_activity or approval_required_event or run_error_event"`: 3 passed, 9 deselected, 1 Starlette deprecation warning. |
| Streaming-first focused file | passed | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py`: 12 passed, 1 Starlette deprecation warning. |
| Required focused pytest | passed | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py`: 67 passed, 1 Starlette deprecation warning. |
| Required broad ruff | caveated | Required broad ruff command still reports 56 pre-existing findings in the phase scope. The run did not mass-fix unrelated lint. |
| Narrow ruff sanity | passed | `uv run ruff check tests/services/assistant/test_agentloop_streaming_first_contract.py`: all checks passed. |
| Undefined-name sanity | passed | `uv run ruff check --select F821,F823 apps/assistant-service/src/assistant_service/core/agent/agent_loop.py tests/services/assistant/test_agentloop_streaming_first_contract.py`: all checks passed. |
| Strict harness validation | passed | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`: quality score 100 after final evidence writeback. |
| NGA-01 completion gate | passed | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --completion-gate --phase NGA-01`: harness validation passed. |
| JSON checks | passed | `feature-oracle.json`, `loop-state.json`, and `loop-contract.json` parsed cleanly after final writeback. |
| `git diff --check` | passed | No whitespace errors after final writeback. |

## Minimal Change Scope

The code patch is additive and scoped to `AgentLoop` event emissions plus focused
tests. No public function signatures, API request/response shapes, database
schemas, frontend routes, deployment scripts, provider credentials, or
production data paths changed.

## Independent Critic Evidence

- F002 critic: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f002-critic.md`
- F003 critic: `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f003-critic.md`

## Caveats and Residual Risk

- The required broad ruff command remains blocked by existing lint debt in the
  wider phase scope. This report treats that as a documented caveat rather than
  unrelated cleanup inside NGA-01.
- Pattern-based redaction is not a complete secret-classification system.
  NGA-05 should add broader negative cases for connector/tool payloads, signed
  URLs, and release logs.
- The approval resume/result round-trip belongs to downstream UI/API work; this
  phase proves the backend `approval_required` pause state and preserves the
  `approval_result` event vocabulary.

## Decision

NGA-01 is complete and NGA-02 is unlocked. The next loop item is `NGA-F004` in
`docs/general_ai_assistant_next_gen/phase-02-skills-and-mcp-capability-layer.md`.
