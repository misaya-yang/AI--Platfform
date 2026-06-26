# NGA-01 F002 Minimum Viable Agent Harness Actor Report

Status: passed

**Phase:** NGA-01 Minimum Viable Agent Harness
**Feature Oracle:** NGA-F002
**Actor:** Codex generator

## Scope

This report covers the active oracle item only: `NGA-F002`. It does not complete all NGA-01 acceptance gates because `NGA-F003` remains a separate pending observability item.

## Plan Followed

1. Loaded the harness runtime files, NGA-00 dependency report, NGA-01 phase contract, and only NGA-01 `PRIMARY_CONTEXT`.
2. Wrote the durable plan at `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-plan.md`.
3. Added failing streaming-first contract tests for missing canonical context-budget and tool lifecycle events.
4. Patched the existing streaming-first `AgentLoop` without adding a second planner or alternate loop.
5. Ran the phase validation commands and recorded the ruff blocker separately from passing behavioral/harness checks.

## Files Changed

- `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py`
- `tests/services/assistant/test_agentloop_streaming_first_contract.py`
- `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-plan.md`
- `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f002-report.md`
- `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f002-critic.md`
- Harness runtime writebacks under `docs/general_ai_assistant_next_gen/`

## Implementation Summary

- `AgentLoop` now emits a `context_budget` event after streaming-first context construction and before the first model stream.
- The `context_budget` payload contains stable identifiers and counts only: run/session IDs, message count, history count, selected tool count/names, prompt/context character counts, file count, and context detail flag.
- The canonical tool lifecycle now emits AG-UI-compatible `tool_call_start`, `tool_call_result`, and `tool_call_end` aliases alongside the existing `tool_call_started` and `tool_call_completed` events.
- Tool alias payloads use stable IDs/status values and avoid raw arguments in the canonical alias path.
- Existing gateway decisions, middleware gates, artifact events, session persistence, and streaming-first behavior are preserved.

## Validation Evidence

| Gate | Command | Result | Notes |
| --- | --- | --- | --- |
| Red test | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py -k "context_budget or tool_artifact"` | failed as expected | 2 selected tests failed because `context_budget` and `tool_call_start` were absent. |
| Targeted green test | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py -k "context_budget or tool_artifact"` | passed | 2 passed, 7 deselected, 1 Starlette deprecation warning. |
| Required focused pytest | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py` | passed | 64 passed, 1 Starlette deprecation warning. |
| Required focused ruff | `uv run ruff check apps/assistant-service/src/assistant_service/core/agent apps/assistant-service/src/assistant_service/core/gateway apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/tool_orchestrator.py apps/assistant-service/src/assistant_service/core/tools/tool_selector.py tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py` | blocked by existing lint debt | Reported 56 existing issues such as import ordering and unused locals in broad phase paths. The added lines were not the reported causes. |
| Touched test ruff | `uv run ruff check tests/services/assistant/test_agentloop_streaming_first_contract.py` | passed | All checks passed. |
| Undefined-name sanity | `uv run ruff check apps/assistant-service/src/assistant_service/core/agent/agent_loop.py tests/services/assistant/test_agentloop_streaming_first_contract.py --select F821,F823` | passed | All checks passed. |
| Strict harness validation | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | passed | Quality score 100. |
| Harness JSON | `python3 -m json.tool ...` for feature oracle, loop state, and loop contract | passed | JSON parsed cleanly. |
| Whitespace | `git diff --check` | passed | No whitespace errors. |

## Minimal Change Scope

The implementation stayed inside NGA-01 likely edit paths. It did not touch frontend files, database migrations, deployment scripts, provider credentials, env files, production data, or MCP tenant policy defaults.

The code change is intentionally additive: it emits canonical event aliases and budget telemetry on the existing streaming-first path. It does not add a dependency, change public API signatures, introduce a planner-by-default path, or create a second agent loop.

## Compliance Notes

- No secrets, tokens, connection strings, auth headers, signed URLs, provider keys, private documents, or production data were read or printed.
- The new `context_budget` event records counts and selected tool names, not prompt text or raw user message content.
- The canonical tool lifecycle aliases avoid raw tool arguments; the legacy `tool_call_started` event is preserved unchanged for compatibility.
- AI behavior was validated with fake model/tool fixtures only.

## Feature Oracle Updates

| Feature ID | Old Status | New Status | Evidence |
| --- | --- | --- | --- |
| NGA-F002 | failing | passing | This actor report and `docs/general_ai_assistant_next_gen/reports/nga-01-minimum-viable-agent-harness-f002-critic.md`. |

## Handoff

`NGA-F002` is passed. `NGA-F003` remains failing and must be executed as the next feature-oracle item before NGA-01 can be considered fully passed or before NGA-02 can start.
