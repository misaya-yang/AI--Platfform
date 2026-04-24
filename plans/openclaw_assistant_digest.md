# OpenClaw Assistant Integration Digest

Date: 2026-02-25
Scope: AI Assistant (generic), not playground vertical agents.

## Key references used
- OpenClaw docs (concepts, queue, HITL, security): https://docs.openclaw.ai/
- OpenClaw system run node: https://docs.openclaw.ai/tools/system.run
- OpenClaw browser node: https://docs.openclaw.ai/tools/browser
- MCP spec: https://modelcontextprotocol.io/specification/2025-11-05
- OpenAI Agents Python HITL: https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md

## Practical principles extracted
1. Context should be budget-driven, with explicit compaction signals and deterministic assembly order.
2. Tool execution should pass a strict invocation context (tenant/user/session/run/scope), no system fallback.
3. Risky tools should run behind policy + approval workflow (interrupt/resume semantics).
4. Queue-based command scheduling should support dedup key, lease, status transitions, and audit trail.
5. Run lifecycle should be first-class (start/finish/error), queryable by API.

## Direct implementation impact in local code
- Added assistant gateway stack (policy/router/execution gateway).
- Added queue + approval + run API surfaces.
- Added context budget/compaction events and planning.
- Added memory metadata model support: namespace/TTL/sensitivity/source.
- Added strict tool permission/context enforcement and removed orchestration fallback identity path.

## Compatibility constraints kept
- Existing `/assistant/chat` and `/assistant/chat/stream` remain available.
- New capabilities are additive via fields/events/endpoints.
