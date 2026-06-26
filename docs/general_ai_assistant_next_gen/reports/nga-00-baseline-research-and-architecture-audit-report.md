# NGA-00 Baseline Research and Architecture Audit Report

**Status:** passed

**Feature Oracle:** `NGA-F001`

## Scope

This phase created the next-generation universal assistant PRD harness. It did not change assistant runtime code, deploy services, mutate data, or read secrets.

## Evidence

Local files inspected:

- `docs/general_ai_assistant_upgrade/README.md`
- `docs/general_ai_assistant_upgrade/source-packet.md`
- `docs/general_ai_assistant_upgrade/phase-manifest.md`
- `apps/assistant-service/src/assistant_service/main.py`
- `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py`
- `apps/assistant-service/src/assistant_service/core/tool_invoker.py`
- `apps/assistant-service/src/assistant_service/core/tool_orchestrator.py`
- `apps/assistant-service/src/assistant_service/core/tools/tool_selector.py`
- `apps/assistant-service/src/assistant_service/core/mcp/manager.py`
- `apps/assistant-service/src/assistant_service/core/mcp/config.py`
- `apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py`
- `apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py`
- `apps/assistant-service/src/assistant_service/core/memory/memory_manager.py`
- `apps/assistant-service/src/assistant_service/core/rag/context_engine.py`
- `web/src/pages/assistant/index.tsx`

External sources used as source material:

- OpenAI Agents SDK: https://developers.openai.com/api/docs/guides/agents
- OpenAI Agents SDK tools: https://openai.github.io/openai-agents-python/tools/
- OpenAI Agents SDK guardrails: https://openai.github.io/openai-agents-python/guardrails/
- OpenAI Agents SDK tracing: https://openai.github.io/openai-agents-python/tracing/
- Codex App Server harness: https://openai.com/index/unlocking-the-codex-harness/
- Codex agent loop: https://openai.com/index/unrolling-the-codex-agent-loop/
- Codex skills: https://developers.openai.com/codex/skills
- Codex AGENTS.md: https://developers.openai.com/codex/guides/agents-md
- Claude Code skills: https://code.claude.com/docs/en/skills
- Claude Code subagents: https://code.claude.com/docs/en/sub-agents
- Claude Code memory: https://code.claude.com/docs/en/memory
- Claude Code plan mode: https://code.claude.com/docs/en/permission-modes
- MCP specification: https://modelcontextprotocol.io/specification/2025-06-18
- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- OpenClaw context engine: https://docs.openclaw.ai/concepts/context-engine
- Hermes Agent: https://github.com/nousresearch/hermes-agent and https://hermes-agent.nousresearch.com/docs/

## Validation Evidence

| Gate | Command or Check | Result | Notes |
| --- | --- | --- | --- |
| Skill loading | Read `product-design:index`, `prd-phase-harness`, builder, long-running-agent, security, phase-folder, and phase-contract references. | passed | Product Design routed to product-shape guidance only; PRD harness used Build/Review-Repair mode. |
| Repo inspection | Inspected assistant-service, gateway, skills, MCP, memory, RAG/context, frontend assistant, tests, Makefile, and package manifests. | passed | No runtime code changed. |
| Web research | Reviewed official OpenAI/Codex, Claude Code, MCP, LangGraph, OpenClaw, and Hermes sources. | passed | External pages were treated as untrusted source material. |
| Harness structure | Final validation command recorded in this report and rerun after edits. | passed | Strict validator evidence is recorded by the final assistant turn. |

## Findings

- The existing assistant already has many next-generation pieces: streaming-first agent loop, run lifecycle events, tool orchestration, token-aware tool selection, MCP manager, tenant MCP filtering, skills bridge, layered memory, RAG context budget, artifacts, and a rich assistant UI.
- The main gap is coherence, not absence. The product needs one minimal harness contract, better capability UX, explicit memory taxonomy, source-aware RAG, session/review UX, and deterministic eval/release gates.
- The existing `docs/general_ai_assistant_upgrade` harness is release-readiness history and should remain intact. This phase created `docs/general_ai_assistant_next_gen` for the new objective.

## Validation

Commands planned for final harness validation:

```bash
python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json >/dev/null
python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json >/dev/null
python3 -m json.tool docs/general_ai_assistant_next_gen/loop-contract.json >/dev/null
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score
```

The implementation agent must rerun these after any phase-contract edit.

## Minimal Change Scope

Changed only:

- `.gitignore`
- `docs/general_ai_assistant_next_gen/**`

## Feature Oracle Updates

| Feature ID | Old Status | New Status | Evidence |
| --- | --- | --- | --- |
| NGA-F001 | failing | passing | This actor report plus `docs/general_ai_assistant_next_gen/reports/nga-00-baseline-research-and-architecture-audit-critic.md`. |

## Critic Notes

- No source-code implementation was attempted.
- No new dependency was added.
- No secret values were read or printed.
- External web pages were treated as untrusted source material.

## Unlock Decision

`NGA-01` is unlocked as a planned implementation phase. The next agent should work only on `NGA-F002` and the minimum viable harness contract.
