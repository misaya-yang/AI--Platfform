# Phase 02 - Discovery-first tools

- PHASE_ID: AGA-02
- FEATURE_ID: AGA-F003
- DEPENDS_ON: none

## Outcome

Safe profile first turn advertises discovery bridges. Other tools stay callable via `tool_search` / `tool_call`.

## Scope

In: `tool_selector.py`, `agent_context_lifecycle.py`

Out: approval/policy

## Done when

- [x] Discover mode hides spawn/memory/MCP schemas
- [x] Allowlisted and bound skill tools can still be first-turn visible

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Selector | `pytest tests/services/assistant/test_tool_selector.py` | Advertisement |

## Stop or confirm

- none
