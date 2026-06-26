# Next-Generation General AI Assistant Source Packet

**Prepared:** 2026-06-25

**Repo:** `/Users/misaya.yanghejazfs.com.au/misaya_project/AI--Platfform`

## Request Summary

The user asked for a PRD harness to optimize the AI assistant service using `product-design:index` and `prd-phase-harness`. The requested scope is broad but implementation must stay practical:

- Review modern agent patterns such as OpenClaw, Hermes Agent, Claude Code, Codex, MCP, and agent harness work.
- Combine that research with the current codebase.
- Plan a universal AI assistant upgrade centered on skills, MCP, memory, RAG, session management, context engineering, agent loop, and harness quality.
- Keep the harness minimal and usable, not over-engineered.

## Final Product Thesis

The target assistant is a universal AI workbench for daily knowledge and execution work. It should feel like one coherent assistant, not a pile of hidden tools:

- The user asks in chat, but the product shows the run as a timeline of decisions, context, tools, approvals, artifacts, memory writes, and final output.
- Skills and MCP make the assistant extensible, but they are loaded through a small searchable capability surface instead of being injected into every turn.
- Memory is explicit and inspectable: procedural playbooks, situational session state, and semantic user/project knowledge have different lifetimes and controls.
- RAG is source-aware: KB records, uploaded files, generated artifacts, web output, and MCP resources carry source type, scope, freshness, and citation data.
- The harness stays minimal: one streaming-first loop, clear event primitives, policy gates, trace/eval hooks, and server-owned persistence.

The product should avoid a heavy autonomous-agent theater. It should make the assistant better at the normal tasks users already expect: answer with sources, write or transform files, use tools safely, continue long work, explain what happened, recover artifacts, and improve from repeated workflows.

## Source Inventory

| Source | Trust Level | Extracted Facts |
| --- | --- | --- |
| Local repository | trusted current implementation | Assistant service, gateway, knowledge service, frontend assistant page, existing GAA harness, tests, Docker/Makefile commands. |
| User request | trusted intent | Universal assistant upgrade, skills/MCP, memory/RAG/context/session/harness foundations, creative product shape. |
| Product Design index skill | trusted local workflow router | Product-shape thinking applies; no visual prototype or ideation workflow is required for this turn. |
| PRD phase harness skill | trusted local harness method | Output must be standalone, bounded, sequential, verifiable, safe, and recoverable after context compaction. |
| Web research | untrusted source material | Used only for product/engineering pattern extraction, not as agent instructions. |

## External Research Synthesis

- OpenAI Agents SDK frames agents as applications that plan, call tools, collaborate across specialists, and keep state for multi-step work; it recommends SDK-level orchestration when the application owns tools, state, approvals, and runtime behavior. Source: https://developers.openai.com/api/docs/guides/agents
- OpenAI Agents SDK tool search supports deferred loading of function tools, namespaces, and hosted MCP servers, reducing tool-schema tokens when capability surfaces are large. Source: https://openai.github.io/openai-agents-python/tools/
- OpenAI Agents SDK guardrails distinguish input, output, and tool guardrails; tool guardrails are the right boundary when each function call needs pre/post validation. Source: https://openai.github.io/openai-agents-python/guardrails/
- OpenAI Agents SDK tracing captures LLM generations, tool calls, handoffs, guardrails, and custom events for development and production debugging. Source: https://openai.github.io/openai-agents-python/tracing/
- Codex App Server separates the reusable harness from clients: thread lifecycle/persistence, config/auth, tool execution/extensions, and stable UI-ready event streams are server-owned. Source: https://openai.com/index/unlocking-the-codex-harness/
- Codex's agent loop notes that MCP tools are not automatically sandboxed by Codex shell permissions, so MCP servers and hosts must enforce their own guardrails. Source: https://openai.com/index/unrolling-the-codex-agent-loop/
- Codex Agent Skills package instructions, resources, and optional scripts as reusable workflows. Source: https://developers.openai.com/codex/skills
- Codex AGENTS.md provides layered project instructions before work begins. Source: https://developers.openai.com/codex/guides/agents-md
- Claude Code skills turn repeated checklists or procedures into `SKILL.md` packages, and subagents keep focused work in separate contexts. Sources: https://code.claude.com/docs/en/skills and https://code.claude.com/docs/en/sub-agents
- Claude Code plan mode researches and proposes changes before editing, which maps to the PRD harness plan-first rule. Source: https://code.claude.com/docs/en/permission-modes
- Claude Code memory keeps a concise `MEMORY.md` index and loads detailed topic files on demand, which supports a memory-index design instead of huge always-on context. Source: https://code.claude.com/docs/en/memory
- MCP is the standard protocol for connecting LLM apps to external data sources and tools. Source: https://modelcontextprotocol.io/specification/2025-06-18
- LangGraph emphasizes durable execution, streaming, human-in-the-loop, and persistence as orchestration primitives. Source: https://docs.langchain.com/oss/python/langgraph/overview
- OpenClaw's context engine controls message inclusion, summarization, and subagent boundaries. Source: https://docs.openclaw.ai/concepts/context-engine
- Hermes Agent emphasizes a learning loop that creates skills from experience, improves them, persists knowledge, and searches past conversations. Source: https://github.com/nousresearch/hermes-agent and https://hermes-agent.nousresearch.com/docs/

## Research-to-Implementation Principles

These are the final research conclusions that should guide implementation:

| External Pattern | Adopt | Avoid |
| --- | --- | --- |
| Codex App Server thread/turn/item model | Server-owned run primitives that the UI can resume and render consistently. | Browser-owned state for long-running work. |
| OpenAI tool search and namespaces | Deferred loading for large tool, skill, and MCP surfaces. | Sending every capability schema in every model call. |
| Tool guardrails and approvals | Per-tool pre/post validation, approval pauses, and visible denied states. | Agent-level guardrails as the only boundary for tool execution. |
| Traces and spans | Trace IDs, grouped conversation IDs, and custom events for eval/debug. | Plain text logs as the only observability layer. |
| Claude Code plan mode and subagents | Plan-first only for ambiguous or high-risk runs; separate critic context for completion. | Planner-by-default latency on simple chat turns. |
| Claude/Codex/Hermes skills | On-demand `SKILL.md` style workflows with resources and scripts. | Auto-enabled generated skills without review, eval, and rollback metadata. |
| MCP specification | MCP as a standard connector protocol with host/client/server boundaries. | Treating MCP tools as trusted just because they are connected. |
| LangGraph durable execution | Persistence, streaming, and human-in-the-loop as harness capabilities. | Replacing the current assistant-service before proving a local gap. |
| OpenClaw context engine | Explicit context assembly, summarization, and subagent-boundary policy. | Unbounded chat history and tool outputs in every prompt. |
| Hermes memory loop | Curated memory and procedural learning with user-visible controls. | Silent long-term memory writes with no inspect/delete path. |

## Current System Shape

- Public gateway entrypoint: `src/main.py`, with assistant gateway/proxy routes under `src/api/v1/assistant.py` and `src/api/v1/_assistant_proxy.py`.
- Assistant runtime: `apps/assistant-service/src/assistant_service`, internal port `8093`.
- Knowledge runtime: `apps/knowledge-service/src/knowledge_service`, internal/local port `8092`.
- Shared primitives: `packages/ai-gateway-core/src/ai_gateway_core`.
- Frontend assistant workspace: `web/src/pages/assistant`.
- Existing release/readiness harness: `docs/general_ai_assistant_upgrade`; keep it as historical stability evidence, not the next-generation PRD.

## Current Assistant Code Facts

- `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py` is the central assistant loop. It now runs a streaming-first path; the old 8-step pipeline is documented in comments but the code path states the legacy pipeline was removed.
- `AgentLoopConfig` already carries harness-facing controls: `execution_profile`, `memory_mode`, `os_agent_enabled`, `runtime_mode`, `queue_mode`, `context_detail`, `skills_enabled`, and `memory_profile`.
- `AgentLoop` emits run lifecycle and UI events such as `run_started`, `run_finished`, `run_error`, `gateway_decision`, `context_budget`, `context_compacted`, `memory_retrieved`, `skill_selected`, `skill_loaded`, tool lifecycle events, artifacts, and approvals.
- `ToolInvocationContext` carries `session_id`, `user_id`, `tenant_id`, `request_id`, `run_id`, `scope_id`, `policy_profile`, `os_agent_enabled`, KB dataset ids, user context, and metadata.
- `ToolOrchestrator` supports parallel tool groups, dependencies, result caching, in-flight de-duplication, and working memory updates.
- `tool_selector.py` scores tools by relevance and token budget, always includes core KB/memory/subagent tools, and has MCP server keyword handling for `docgen`.
- `MCPManager` connects configured MCP servers, registers `mcp_{server}__{tool}` tools, maps resource outputs into artifacts, and sanitizes descriptions.
- `TenantMCPConfigService` filters MCP tools by tenant policy, but DB-miss currently defaults to all known servers. A future phase should make this default explicit and testable.
- Skills are partially implemented: `ASSISTANT_RUNTIME_SKILLS` enables runtime skill selection; `SkillToolBridge` registers `skill_*` tools from manifests; bundled filesystem skills exist for docx, pdf, pptx, xlsx, and skill creation.
- Memory exists in multiple layers: `MemoryManager` has working, session, and long-term memory; runtime v2 components include memory source store, indexer, retriever, reflector, PII filter, and scheduler flags.
- `MemorySourceStore` persists markdown memory under per-tenant and per-user paths, including daily memory, long-term `MEMORY.md`, and reflection files.
- `ContextAssemblerV2` already builds model messages with budget plans and cost attribution for tool definitions, injected files, skill metadata, and memory snippets.
- RAG/context foundations exist: `ContextBudgetManager`, stable-prefix context construction, scenario analysis, query intent analysis, scenario-aware retrieval, RAG metrics, and file processing. `FileProcessor.create_session_kb` still records an open implementation item for session-level temporary KB creation.
- Assistant UI already includes Activity, Timeline, Tasks, Artifacts, Connectors, Customize, Model/KB selectors, WebSearch toggle, prompt suggestions, generated document/image/quiz views, and conversation sidebar. `ConnectorsPanel` count still depends on a pending implementation path in `index.tsx`.

## NGA-01 F002 Implementation Code Facts

- `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py` remains the single canonical streaming-first loop. No second agent loop or planner-by-default path was added.
- The streaming-first path now emits `context_budget` after context construction and before the first model stream. The payload carries run/session IDs, message and history counts, selected tool count/names, prompt/context character counts, file count, and context detail state.
- The new `context_budget` payload intentionally excludes raw prompt text, raw user message content, auth headers, provider keys, signed URLs, and credential-bearing data.
- Tool execution now emits canonical `tool_call_start`, `tool_call_result`, and `tool_call_end` aliases alongside the existing `tool_call_started` and `tool_call_completed` events.
- Canonical tool lifecycle aliases use stable `tool_call_id`, `name`, `status`, duration/error fields, and avoid raw tool arguments in the alias payload.
- `tests/services/assistant/test_agentloop_streaming_first_contract.py` now verifies the redacted context-budget event and AG-UI-compatible tool lifecycle aliases with fake model/tool fixtures.

## NGA-01 F003 Implementation Code Facts

- Streaming-first trace and activity events now carry common run/session
  correlation fields across lifecycle, gateway, queue, sandbox, approval, tool,
  artifact, context compaction, finish, and error paths.
- `artifact_created` events are enriched at the `AgentLoop` boundary with the
  creating `tool_call_id` and `tool_name`, giving UI timelines and release
  reviews a direct artifact-to-tool relationship.
- Event-facing auth/key/token/password patterns are redacted in
  `streaming_first_started.message_preview`, legacy `tool_call_started`
  arguments, approval/policy reasons, tool result previews, tool error fields,
  and streaming-first run-error payloads.
- Focused tests now cover successful tool/artifact activity records,
  approval-required pause state records, and provider-failure `run_error`
  redaction using fake model/tool fixtures.
- `approval_result` remains in the frozen stream-event vocabulary. NGA-01 proves
  backend pause-state evidence through `approval_required`; UI/API resume
  behavior is downstream work.

## NGA-02 F004 Implementation Code Facts

- `apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py`
  remains the bridge from runtime skill manifests to callable `skill_*` tools.
- Registered skill tools now carry bounded `capability_metadata` with kind,
  skill name, title, summary, version, source, tags, setup state, risk level,
  trigger examples, and progressive-disclosure state.
- Skill capability metadata deliberately does not serialize full skill
  instructions, references, scripts, or tool schema bodies. It marks
  `level2_loaded` as `False` and `instructions_loaded_on_demand` as `True`.
- Skill selection now gets `relevance_keywords` from level-0 skill metadata and
  trigger patterns, so token-aware tool selection can match skills without
  loading full instructions.
- Skill tool result metadata now includes `skill_name`, `skill_version`, and
  `skill_source` for downstream trace/audit inspection.
- `apps/assistant-service/src/assistant_service/api/routes/tools.py` now has an
  additive `_tool_catalog_entry` serializer. The existing `/tools` keys are
  preserved, and capability catalog fields are added for users/operators.
- NGA-F004 did not change frontend connector UI, MCP tenant policy, generated
  skill proposal workflow, database schema, migrations, deployments, env files,
  or the NGA-01 event contract.

## NGA-02 F005 Implementation Code Facts

- `apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py`
  now makes missing MCP tenant policy explicit and fail-closed. No database
  handle, a failed policy load, or a missing tenant row produces an empty
  `allowed_servers` set.
- `TenantMCPConfig` now records `policy_source` with values such as
  `configured`, `default_deny_no_database`, `default_deny_missing_config`, and
  `default_deny_config_error`.
- `RegistryToolInvoker` now audits denied MCP calls and returns without
  executing the external MCP tool when tenant policy denies access or policy
  lookup fails.
- `create_tool_invoker()` now installs a default-deny
  `TenantMCPConfigService(database=None)` when no tenant MCP service is supplied.
- `MCPManager` now publishes bounded MCP `capability_metadata`, tenant policy
  scope, setup state, trigger examples, and progressive-disclosure state for
  registered `mcp_{server}__{tool}` tools.
- MCP catalog descriptions strip control characters, stay bounded, and redact
  credential-shaped strings before exposure through `/tools`.
- The `/tools` catalog serializer includes safe MCP metadata fields such as
  `mcp_server`, `mcp_tool`, `policy_scope`, and `external_service`.
- NGA-F005 did not change frontend connector UI, database schema, migrations,
  live connector credentials, provider dashboards, deployments, generated skill
  enablement, or the NGA-01 event contract.

## NGA-02 F006 Implementation Code Facts

- The assistant-service skill registry, builder, executor, parser, and
  `skill_create` files are shims. The canonical generated-skill implementation
  now lives in `packages/ai-gateway-core/src/ai_gateway_core/skills`.
- `SkillManifest` now carries generated-skill lifecycle metadata:
  `generated`, `lifecycle_status`, `review`, `evaluation`, and `rollback`.
- Generated skills expose deterministic activation checks for independent
  critic evidence, eval evidence, and rollback metadata.
- `parse_skill_md` treats user SKILL.md content as generated by default and
  keeps it `proposed` plus disabled until activation gates are present.
- `SkillRegistry.register()` and `SkillRegistry.save_manifest()` fail closed for
  generated skills by storing proposed disabled manifests when required
  activation evidence is missing.
- `SkillRegistry.save_manifest()` reuses existing `assistant_skills.status` and
  `assistant_skill_versions.status` columns for proposed/active state; no schema
  migration was introduced.
- Skill catalog metadata now exposes generated-skill `lifecycle_status`,
  `review_required`, and activation requirement booleans without serializing full
  instructions.
- `skill_create` now instructs the model to produce proposed disabled SKILL.md
  output and not register or enable generated skills before independent critic,
  eval, and rollback evidence exists.
- NGA-F006 did not change frontend connector UI, database schema, migrations,
  live connector credentials, provider dashboards, deployments, or the NGA-01
  event contract.

## NGA-03 F007 Implementation Code Facts

- `apps/assistant-service/src/assistant_service/core/memory/memory_manager.py`
  now defines explicit `MemoryProfile` values `off`, `basic`, and `hybrid`.
- `MemoryManager` defaults to `hybrid` for backward compatibility, while
  stricter profiles can be passed at construction.
- `MemoryManager` now defines `MemoryType` values `procedural`,
  `situational`, and `semantic`, and writes memory boundary metadata with
  memory type, active profile, tenant/user/session scope, privacy filter flags,
  and `trust: untrusted_memory_data`.
- The `off` profile blocks long-term writes and recall but still allows
  explicit long-term deletion.
- The `basic` profile allows semantic long-term facts/preferences and blocks
  procedural memory.
- The `hybrid` profile permits procedural memory only as proposed metadata,
  preserving the NGA-F006 generated-skill review gate.
- Memory values are filtered for email/phone PII and common prompt-control
  phrases before persistence, search-result exposure, and long-term recall
  exposure.
- `MemoryManager.inspect_memory_policy()` exposes storage, retrieval, inspect,
  delete, retention, scope, and privacy boundaries without exposing memory
  values.
- `apps/assistant-service/src/assistant_service/core/runtime/memory/source_store.py`
  now has `delete_source()` for markdown memory sources, confined to the active
  tenant/user root.
- `apps/assistant-service/src/assistant_service/core/tools/memory_tool.py` now
  honors profile and memory-type policy, supports an `inspect` action, sanitizes
  stored values, and does not echo raw memory values in tool results.
- `tests/services/assistant/test_memory_manager.py` now covers the memory
  profiles, tenant scope, PII filtering, prompt-injection boundaries, explicit
  recall/delete, source-store delete confinement, and memory-tool boundaries.
- NGA-F007 did not change frontend files, database schema, migrations,
  knowledge-service ingestion internals, live KB data, provider credentials,
  deployments, or the NGA-01/NGA-02 contracts.

## NGA-03 F008 Implementation Code Facts

- `apps/assistant-service/src/assistant_service/core/files/file_processor.py`
  now collects long uploaded documents that require RAG during `process_files`.
- When long documents exist, `process_files` calls `create_session_kb()` and
  stores the returned dataset ID on `ProcessedFiles.session_kb_id`.
- `create_session_kb()` uses an injected KB proxy method named
  `create_session_dataset` when available, passing the active user, session ID,
  document paths, and metadata.
- Session-KB metadata records `source_type: session_file`, freshness, and
  tenant/user/session scope. Missing or unsupported KB proxies return `None`
  without reading live KB data or changing schemas.
- `apps/assistant-service/src/assistant_service/core/rag/scenario_aware_retriever.py`
  now formats bounded source metadata from retrieval results: source type,
  citation, freshness, dataset ID, chunk ID, tenant ID, user ID, and session ID.
- RAG source metadata values are normalized to one line and capped before being
  added to model context to reduce context bloat and untrusted-source impact.
- `tests/services/assistant/test_context_engine.py` now covers long-upload
  session-KB handoff and source-aware formatted RAG context.
- Mechanical lint cleanup was applied inside NGA-03 validation paths so the
  required broad NGA-03 ruff command passes.
- NGA-F008 did not change frontend files, database schema, migrations,
  knowledge-service ingestion internals, live KB data, provider credentials,
  deployments, or the NGA-01/NGA-02 contracts.

## NGA-03 F009 Implementation Code Facts

- `apps/assistant-service/src/assistant_service/core/rag/context_engine.py`
  now records `CONTEXT_PACKET_ORDER` in budget events together with compaction
  details.
- `apps/assistant-service/src/assistant_service/core/runtime/context/assembler.py`
  now accepts optional bounded source summaries, tool-result summaries,
  artifact summaries, and compaction summary.
- Request context preserves existing RAG/current context first, then appends
  source summaries, recent tool results, recent artifacts, compaction summary,
  and finally the current user query through the existing context engine.
- Summary items are normalized to one line and capped before insertion so raw
  long tool outputs, full artifacts, or full skill instructions are not injected
  into every turn.
- `apps/assistant-service/src/assistant_service/core/runtime/context/cost_breakdown.py`
  now attributes source summaries, tool results, artifacts, and compaction
  summaries as separate contributor categories.
- `tests/services/assistant/test_context_engine.py` now covers ordered packet
  assembly, bounded summaries, compaction telemetry, and contributor categories.
- NGA-F009 did not change frontend files, database schema, migrations,
  provider credentials, deployments, production KB data, or the NGA-01/NGA-02
  contracts.

## Product Requirements

### R1 Universal Assistant Shape

The assistant must become a daily workbench, not only a chat box. It should support:

- Ask/answer with sources.
- Plan/review/execute modes.
- Tool and skill execution with approvals.
- Files, documents, image generation, quiz/exam/content generation, and KB workflows.
- Session resume, branch/fork, share, and artifact recovery.
- Transparent activity timeline and trace-backed explanations of what happened.

Final product modes:

- Direct Answer: fast chat, no extra planner, minimal tools.
- Research: RAG, web/MCP sources, citations, freshness, and source summary.
- Agent Run: visible plan, tool gates, approvals, trace, and artifacts.
- Review: critic-style verification of generated output, files, or code.
- Learn: propose a memory or skill update from repeated user-approved workflows.

### R2 Minimal Agent Harness

The next implementation should converge on one canonical streaming-first loop:

```text
observe -> route policy -> assemble context -> select capabilities -> call model -> gate/execute tools -> persist events/artifacts/memory -> compact -> evaluate -> decide
```

Do not revive a heavy planner-by-default loop. Planning, subagents, and RAG should be invoked when the request or policy requires them.

Minimum viable harness primitives:

- Thread: durable conversation/session container that can resume, fork, share, archive, and recover artifacts.
- Turn: one user-initiated unit of work with a start, active, paused, completed, blocked, or failed state.
- Item: typed event inside a turn, such as model output, tool call, approval, artifact, memory write, context budget, or trace summary.
- Approval: server-owned pause point for sensitive tool, MCP, skill, memory, file, or data actions.
- Artifact: generated document, image, quiz, code output, MCP resource, or file transform with ownership and retention metadata.
- Memory Write: proposed or committed procedural, situational, or semantic update with source, scope, and delete controls.
- Trace: stable run ID plus grouped spans/events for model calls, tool calls, guardrails, approvals, and errors.

### R3 Skills and MCP Experience

Skills and MCP must move from hidden runtime plumbing to a usable capability layer:

- Catalog, discover, enable, disable, and test skills/MCP servers.
- Progressive disclosure of skill metadata and instructions.
- Tenant-scoped MCP policy and per-tool risk labels.
- User-visible install/setup errors without leaking secrets.
- Audit trail for tool, skill, and MCP invocations.

Capability experience requirements:

- Level 0: compact catalog containing name, description, category, risk, setup state, and trigger examples.
- Level 1: selected skill/MCP detail containing parameters, permissions, setup health, and examples.
- Level 2: full skill instructions, references, scripts, or MCP schema loaded only when the run selects that capability.
- Generated skills stay in `proposed` state until review, eval, and rollback evidence are recorded.
- MCP server access should be explicit per tenant; DB-miss behavior must be tested and surfaced as policy, not left as accidental allow-all behavior.

### R4 Memory, RAG, and Context

Memory must be split into explicit classes:

- Procedural memory: reusable workflows, skills, corrections, and playbooks.
- Situational memory: active session, working task state, current files/artifacts, approvals, and recent tool results.
- Semantic memory: durable user/project facts, preferences, and knowledge.

RAG must treat KB, uploaded files, generated artifacts, and web/MCP outputs as scoped sources with citations, freshness, tenant boundaries, and context budgets.

Memory profile requirements:

| Profile | Behavior | Required UX |
| --- | --- | --- |
| off | No long-term writes; situational state remains only for the active run/session. | Show memory disabled state. |
| basic | User-approved semantic facts and preferences only. | Inspect, delete, and source link controls. |
| hybrid | Procedural, situational, and semantic memory with proposed skill/memory updates. | Review queue, approval state, and rollback metadata. |

Context packet order:

1. Stable system and policy prefix.
2. Current user turn and active session state.
3. Selected capability metadata only.
4. Scoped memory snippets with source and profile.
5. RAG snippets with citation, freshness, and tenant/session scope.
6. Recent tool/artifact summaries, not full raw outputs unless selected.
7. Compaction summary and budget telemetry.

### R5 Evaluation and Release

Every new assistant capability needs deterministic evidence:

- Golden tasks for normal use.
- Negative cases for prompt injection, private data, untrusted MCP/skill output, over-broad file access, and unsafe writes.
- Trace/event assertions for the loop.
- Browser evidence for the assistant UI.
- Deployment gates with rollback and no-secret logs.

Golden task families:

- Fast answer with no tool call, proving direct mode latency and event shape.
- KB-grounded answer with citations and source boundaries.
- Uploaded file analysis with session-limited retrieval.
- Skill-selected document generation or transformation.
- MCP-selected artifact creation through local/mock server fixtures.
- Approval-required write or external action that pauses and resumes safely.
- Memory recall and memory deletion behavior.
- Session resume/fork/share with artifact continuity.
- Negative prompt-injection case from web/MCP/file content.
- Release smoke with no live secret printing.

## Non-Goals

- No production deployment in this PRD.
- No schema migration execution without a phase-specific approval gate.
- No new agent framework replacement unless a phase proves the existing modules cannot satisfy the requirement.
- No always-on huge memory or skill prompt.
- No autonomous skill self-installation without user approval and sandbox policy.

## Risk Inventory

| Risk | Impact | Required Gate |
| --- | --- | --- |
| MCP server/tool prompt injection | Tool misuse, data exfiltration | Tool guardrails, tenant allowlist, sanitized descriptions, audit evidence. |
| Skill self-improvement without review | Unsafe code/instructions | Approval workflow, eval before enablement, rollback to previous skill version. |
| Context bloat | Higher cost, worse answers | Tool search/progressive disclosure, memory index, context budget events. |
| Legacy loop confusion | Fragile execution paths | One canonical streaming-first contract and tests for event semantics. |
| Long-term memory privacy | PII leakage or unwanted recall | Explicit memory profile, delete/inspect controls, PII filter, tenant scope. |
| RAG over-retrieval | Wrong citations or stale context | Source-scoped retrieval, freshness metadata, source attribution tests. |
| UX hides agent state | Users cannot trust actions | Activity timeline, approvals, capability state, trace/event detail. |
| Generated skill supply-chain risk | Malicious or brittle procedures become tools | Proposed state, critic review, eval evidence, permission labels, rollback metadata. |
| Memory over-personalization | Wrong recall or unwanted profiling | Profile controls, explicit writes, inspect/delete, source attribution. |
| Server/client state drift | Long-running runs disappear or UI lies | Server-owned thread/turn/item records and reconnect tests. |

## Baseline Evidence Captured This Turn

- Read Product Design index skill and selected it as product-shape guidance only, because no prototype or visual target was requested.
- Read PRD phase harness skill plus builder, long-running agent, and security references.
- Inspected `docs/general_ai_assistant_upgrade`, assistant-service runtime files, tool selector/orchestrator/invoker, MCP manager/config, skills bridge, memory manager, RAG context engine, and frontend assistant page/components.
- Performed external research using official OpenAI, Anthropic/Claude Code, MCP, LangGraph, OpenClaw docs, and Hermes Agent sources.
- Rechecked the updated `prd-phase-harness` validator and upgraded this harness to use explicit `critic` role contracts and independent critic evidence gates.
- Created this next-generation harness under `docs/general_ai_assistant_next_gen`.

## NGA-04 F010 Source Packet Addendum

Frontend assistant UI facts captured for `NGA-F010`:

- `web/src/pages/assistant/hooks/useChatSession.ts` already receives AG-UI
  lifecycle, step, tool, approval, context-budget, context-compaction,
  working-memory, and artifact events. Before F010, legacy
  `task_planning`/`working_memory_update` only updated `workingMemory`; in the
  default V2 UI the legacy task panel is hidden, so those states were not
  inspectable through Activity.
- `web/src/pages/assistant/components/buildTimeline.ts` was the canonical
  transformation feeding the inline Activity pill and right-side Activity
  drawer. Before F010, it derived rows only from thinking, `toolCalls`,
  `toolResults`, and legacy `searchStatus`.
- `web/src/pages/assistant/index.tsx` selected the top-bar Activity target from
  streaming, `toolCalls`, `searchStatus`, or thinking content only; completed
  messages with only `processSummary`, context, or artifact signals could be
  skipped by the top-bar chip.
- `artifact_created` already hydrated the artifacts panel and inline generated
  artifact cards. F010 reuses that existing state and adds Activity timeline
  rows for generated artifacts.
- Mobile already had an artifact bottom sheet but no Activity bottom sheet.
  F010 reuses `ActivityPanel` for mobile instead of introducing a second mobile
  renderer.

Code-summary writeback:

- `buildTimeline()` now renders rows from `processSummary.steps`,
  `processSummary.tools`, context budget/compaction, retrieved contexts, and
  generated artifacts.
- `task_planning` and `working_memory_update` mirror into `processSummary` while
  preserving existing `workingMemory`.
- Approval events can create visible tool rows if no prior tool row exists.
- The top-bar Activity target includes process summaries, contexts, and
  generated artifacts.
- Mobile Activity opens in a bottom sheet using the same `ActivityPanel`.

Boundary decisions:

- No backend event contract or API payload shape was changed.
- No schema migration, deployment, production data, provider credential,
  dependency, or destructive operation was required.
- Full e2e stack validation is environment-limited unless local
  `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are supplied.

## NGA-04 F011 Source Packet Addendum

Frontend assistant session facts captured for `NGA-F011`:

- `useChatSession()` already restores assistant session history, fetches
  persisted session artifacts through `getSessionArtifacts(sessionId)`, hydrates
  assistant messages from persisted `metadata.artifact_ids`, and rebuilds
  `codeExecution.outputFiles` for the most recent artifact-producing assistant
  message.
- Before F011, a restored session with one persisted artifact could expose two
  artifact affordances because `index.tsx` counted
  `artifacts.length + codeExecution.outputFiles.length`. The same artifact ID
  can be present in both arrays after restore.
- `ShareDialog` already creates shares through the existing
  `/api/v1/assistant/sessions/{session_id}/share` client path and includes
  artifacts by default when the user leaves the toggle enabled.

Code-summary writeback:

- `web/src/pages/assistant/index.tsx` now computes a unique artifact affordance
  count from persisted artifact IDs and current-run output-file artifact IDs.
- The desktop Artifacts chip and Share dialog both use that unique count, so a
  restored one-artifact session renders and shares as one artifact.
- `web/e2e/chat-experience.spec.ts` can preload mocked assistant sessions,
  history, artifacts, and share responses for browser regression coverage.
- The focused F011 test proves restored history, inline artifact recovery,
  desktop Artifacts count, Share dialog count, and `include_artifacts: true`
  share payload behavior.

Boundary decisions:

- No session, artifact, share, memory, or backend ownership API contract was
  changed.
- No schema migration, deployment, production data, provider credential,
  dependency, or destructive operation was required.
- The in-app Browser plugin was unavailable for `iab`; Playwright fallback was
  used for rendered desktop and mobile evidence.
- Full e2e stack validation is environment-limited unless local
  `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are supplied.

## NGA-05 F012 Source Packet Addendum

Evaluation, safety, release, and rollback facts captured for `NGA-F012`:

- Existing assistant safety coverage passed through the phase-required pytest
  set: eval safety contracts, guardrails, safe fetch, safe-fetch call sites, and
  tool orchestration.
- Existing integration coverage passed for assistant OpenAPI and core
  isolation. Service failure-isolation tests skipped locally because the
  docker-compose services were not running.
- The exact frontend release command passed: type-check, lint, build, and
  `e2e:opensource`. Lint still reports 39 existing warnings and the build still
  reports the existing Vite large-chunk warning.
- `docker compose --env-file .env.example config --quiet` validates committed
  compose syntax and example env defaults.
- `scripts/new/validate-env.sh` states it checks env files without printing
  secret values. The external env file path is readable, but the config and
  runtime Makefile gates fail before runtime checks because six release settings
  are missing or placeholders: `REDIS_PASSWORD`,
  `DOCGEN_ARTIFACT_SIGN_KEY`, `DEFAULT_USER_PASSWORD`,
  `AUTH_ALLOWED_EMAIL_DOMAIN`, `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and
  `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.

Code-summary writeback:

- No product code changed during NGA-05.
- Release readiness is blocked by environment configuration, not by the
  assistant safety, integration, frontend, or committed compose gates run in
  this phase.
- Whole-demand regression classifies `NGA-F001` through `NGA-F011` as inherited
  passing evidence and `NGA-F012` as blocked on the named release env gate.
- A continuation recheck reran `make validate-config` and `make validate` with
  the specified external `ENV_FILE` path and confirmed the same six-key release
  env blocker before runtime checks.
- A third consecutive goal-turn recheck confirmed the same blocker. The
  remaining work requires operator-side env remediation or an explicit waiver.
- User then instructed "那先不管", so the repeated external env release gate is
  recorded as waived/deferred for harness completion. Production release
  readiness remains unproven until the external env config and runtime gates
  pass.

Boundary decisions:

- No real env values, tokens, connection strings, provider credentials,
  production data, deployment logs, or dashboard state were copied into reports.
- No deployment, package publishing, credential rotation, production migration,
  production data access, dependency change, or destructive git operation was
  performed.
- The upgraded assistant must not be marked production release-ready until the
  external env config and runtime gates pass; the waiver only closes the PRD
  harness execution path for now.
