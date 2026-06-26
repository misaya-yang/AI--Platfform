# NGA-01 Minimum Viable Agent Harness Plan

**Phase:** NGA-01 Minimum Viable Agent Harness
**Feature-oracle item:** NGA-F002, then NGA-F003
**Status:** updated for active NGA-F003 loop item

## Scope

Execute one oracle item only: make the existing streaming-first `AgentLoop` expose a concrete, testable minimum harness contract. This pass must not advance NGA-02 and must not update NGA-F003 oracle state.

## Plan

| Requirement or gate | Likely changed files | Validation | Minimal-change boundary |
| --- | --- | --- | --- |
| R1 canonical run contract | `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py`, `tests/services/assistant/test_agentloop_streaming_first_contract.py` | Focused assistant pytest | Add or normalize events around the existing streaming-first path; do not add a second loop. |
| R2 streaming-first behavior | `tests/services/assistant/test_agentloop_streaming_first_contract.py` | Golden streaming tests | Preserve immediate model streaming and existing `streaming_first_completed` lifecycle. |
| R3 policy and tool boundaries | `agent_loop.py`, focused tests | Focused assistant pytest and ruff | Preserve `ToolInvocationContext`, gateway metadata, and middleware gates. |
| R4 trace/debug evidence | `agent_loop.py`, focused tests | Focused assistant pytest | Emit counts and identifiers only; do not include raw prompt, provider key, auth header, signed URL, or credential-bearing payloads. |
| Evidence and handoff | `docs/general_ai_assistant_next_gen/**` | Strict harness validator and `git diff --check` | Update only the active feature `NGA-F002` in `feature-oracle.json`; keep NGA-F003 for the next loop item. |

## Test-First Method

1. Add a focused contract test that fails because the streaming-first path does not yet emit the required canonical context-budget evidence.
2. Add a focused contract test that fails if tool lifecycle aliases are missing from the canonical event stream.
3. Implement the smallest event-contract patch in `AgentLoop`.
4. Run the full phase validation commands and write the actor and critic artifacts.

## Validation Commands

```bash
uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py
uv run ruff check apps/assistant-service/src/assistant_service/core/agent apps/assistant-service/src/assistant_service/core/gateway apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/tool_orchestrator.py apps/assistant-service/src/assistant_service/core/tools/tool_selector.py tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score
git diff --check
```

## Review Method

Review the final diff against the phase contract and verify that:

- No frontend, database, deployment, secret, or migration file is edited.
- No planner-by-default or second loop abstraction is introduced.
- Event payloads contain stable IDs and counts, not raw prompt or secret-bearing data.
- NGA-01 remains locked after F002 unless NGA-F003 is handled in a separate loop item.

## NGA-F003 Plan

**Active item:** NGA-F003 Trace and activity records

**Assumption:** F002 is already passing; this loop item edits only the existing
streaming-first event contract and focused tests needed for F003.

| Requirement or gate | Likely changed files | Validation | Minimal-change boundary |
| --- | --- | --- | --- |
| Traceable lifecycle and tool activity | `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py`, focused streaming-first test | Focused pytest and golden contract tests | Add run/thread/tool identifiers to existing events; do not add a second event bus or persistence layer. |
| Artifact activity correlation | `agent_loop.py`, focused streaming-first test | Focused pytest | Enrich `artifact_created` payloads at the loop boundary with run and tool call IDs only. |
| Error trace redaction | `agent_loop.py`, focused streaming-first test | Focused pytest | Sanitize event-facing error strings without changing internal exception logging or tool execution behavior. |
| Phase evidence and handoff | `docs/general_ai_assistant_next_gen/**` | Strict harness validator, completion gate, `git diff --check` | Update only NGA-F003 status/evidence plus NGA-01 report, critic artifact, progress, ledger, source packet, handoff, and loop state. |

### NGA-F003 Test-First Method

1. Add a focused contract test that fails when required trace/activity event
   payloads lack `run_id`, `thread_id`, or tool correlation fields.
2. Add a focused contract test that fails when streaming-first error events leak
   a secret-like exception string.
3. Implement the smallest AgentLoop event-payload patch to pass those tests.
4. Run the phase validation commands and record actor plus critic evidence before
   deciding whether NGA-01 can advance.
