# Next-Generation General AI Assistant Continuity Ledger

## Phase Chain

| Phase | Owns | Depends On | Unlocks | Handoff Boundary |
| --- | --- | --- | --- | --- |
| NGA-00 | Research, code inventory, requirements, risks | none | NGA-01 | Source packet, manifest, oracle, and research-backed phase contracts. |
| NGA-01 | Minimal agent harness and run event contract | NGA-00 | NGA-02 | Stable loop, event, policy, tool, persistence, and trace interfaces. |
| NGA-02 | Skills and MCP capability layer | NGA-01 | NGA-03 | Capability catalog, progressive loading, tenant policy, audit, and skill proposal workflow. |
| NGA-03 | Memory, RAG, and context foundation | NGA-02 | NGA-04 | Memory taxonomy, retrieval scope, context budgets, citations, compaction, and privacy boundaries. |
| NGA-04 | Assistant UX and session experience | NGA-03 | NGA-05 | UI surfaces for plan/review, timeline, approvals, capability state, memory, session recovery, and feedback. |
| NGA-05 | Eval, safety, and release gate | NGA-04 | final release decision | Whole-demand regression, deployment readiness, rollback, observability, and residual-risk report. |

## Interface Decisions

- The existing assistant-service remains the runtime owner. Do not introduce a parallel agent framework before proving the current modules cannot satisfy the requirement.
- The canonical loop is streaming-first. Planning, subagents, and RAG should be demand-driven or policy-driven, not default latency added to every turn.
- The assistant UI should render stable run events instead of reverse-engineering raw model text.
- Skills and MCP are both capability sources, but they need separate policies: skills are reusable workflows with resources/scripts; MCP is external tool/data integration with server/tool-level permissions.
- Memory is not a single blob. Procedural, situational, and semantic memory must have separate storage, retrieval, privacy, and UI controls.
- The release gate must not treat harness validation as product validation. Harness validation only proves the PRD files are executable.

## Code Facts Downstream Phases Inherit

- `AgentLoopConfig` already exposes execution, runtime, queue, context, skills, and memory profile controls.
- NGA-F002 implementation added streaming-first `context_budget` events with run/session identifiers and count-only budget telemetry; payloads avoid raw prompt text and raw user message content.
- NGA-F002 implementation added canonical `tool_call_start`, `tool_call_result`, and `tool_call_end` aliases beside the legacy `tool_call_started` and `tool_call_completed` events.
- NGA-F003 implementation added run/session correlation to lifecycle, gateway, queue, sandbox, approval, tool, artifact, context compaction, finish, and error events emitted by the streaming-first loop.
- NGA-F003 implementation ties `artifact_created` payloads back to the creating `tool_call_id` and `tool_name`.
- NGA-F003 implementation redacts common event-facing auth/key/token/password patterns in message previews, legacy tool arguments, policy reasons, result previews, and error payloads.
- `ToolInvocationContext` already carries tenant/user/session/request/run/policy metadata.
- `ToolSelector` already applies relevance scoring and a tool-schema token budget.
- NGA-F004 implementation adds bounded skill catalog metadata, trigger examples,
  version/source visibility, and skill `relevance_keywords` through
  `SkillToolBridge`.
- NGA-F004 implementation adds an additive `/tools` catalog serializer that
  preserves existing fields and exposes skill setup/progressive-disclosure
  state without loading full skill instructions.
- NGA-F005 implementation makes missing MCP tenant policy explicit default-deny,
  records policy source, hides MCP tools from default factory invokers, and
  audits denied MCP calls before any external tool execution.
- NGA-F005 implementation adds bounded MCP capability metadata and credential
  redaction for catalog-visible MCP descriptions.
- NGA-F006 implementation moves generated-skill safety into the canonical
  `ai_gateway_core.skills` package because assistant-service skill files are now
  shims.
- NGA-F006 implementation adds generated-skill lifecycle metadata and activation
  checks for independent critic evidence, eval evidence, and rollback metadata.
- NGA-F006 implementation makes parser and registry paths fail closed: generated
  user SKILL.md content remains proposed and disabled until activation gates
  pass.
- NGA-F006 implementation exposes proposed/review-required state through skill
  catalog metadata without loading full skill instructions.
- NGA-F006 implementation updates `skill_create` to propose reviewable disabled
  skills instead of enabling or registering generated skills during creation.
- `MCPManager` already registers MCP tools and resource outputs, but default tenant policy and setup UX need review.
- `SkillToolBridge` already maps skills to tools, but user/operator experience and eval-before-enable gates are incomplete.
- NGA-F007 implementation makes `MemoryManager` memory profiles explicit:
  `off`, `basic`, and `hybrid`.
- NGA-F007 implementation adds procedural/situational/semantic `MemoryType`
  metadata, tenant/user/session scope metadata, privacy filter flags, and
  `untrusted_memory_data` trust state to memory writes.
- NGA-F007 implementation blocks long-term write/recall under `off`, blocks
  procedural long-term writes under `basic`, and marks procedural memory as
  proposed under `hybrid`.
- NGA-F007 implementation sanitizes memory values for email/phone PII and
  prompt-control phrases before persistence, search-result exposure, and
  long-term recall exposure.
- NGA-F007 implementation adds inspectable memory policy boundaries without
  exposing memory values, scoped runtime markdown source deletion, and memory
  tool profile/type gates.
- `ContextBudgetManager` and context compression exist, but future work must validate budget events, preserved tool results, and progressive capability loading.
- `web/src/pages/assistant` already has Activity, Timeline, Tasks, Artifacts, Connectors, Customize, Model/KB selectors, and chat/session hooks. UX work should extend these surfaces, not create a landing page.
- NGA-F010 implementation makes the assistant Activity timeline render process
  summaries, tool rows, approval rows, context budget/compaction state,
  retrieved contexts, and generated artifacts, including mobile Activity access.
- NGA-F011 implementation deduplicates restored artifact affordance counts
  across persisted session artifacts and reconstructed current-run output files.
- NGA-F011 implementation keeps sharing on existing backend contracts while the
  UI exposes a unique restored artifact count and sends `include_artifacts`
  through the existing share client.

## Writeback Rules

Each phase report must update:

- `source-packet.md`: new files, services, routes, schemas, tests, commands, and runtime constraints discovered during implementation.
- This ledger: interface contracts changed, downstream impact, and any dependency gate opened or blocked.
- `feature-oracle.json`: only status, evidence, and notes for the active feature item.
- `agent-handoff.md`: next concrete action and blocker status.
- `progress-log.md`: session start/end, validation, and clean-state note.

## Current Active Target

- Phase: `NGA-05`
- Feature: `NGA-F012`
- Completion report: `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md`

## NGA-01 Handoff Decision

- `NGA-F002` is passing with actor and critic evidence.
- `NGA-F003` is passing with actor and critic evidence.
- `NGA-01` is passed with a documented broad-ruff caveat for pre-existing lint debt.
- `NGA-02` is unlocked. Start with `NGA-F004` only.

## NGA-02 NGA-F004 Handoff Decision

- `NGA-F004` is passing with actor and critic evidence.
- Skills now expose bounded capability metadata and trigger-derived selection
  keywords without loading full instructions into the catalog.
- `/tools` exposes additive catalog fields while preserving the existing flat
  tool-list keys.
- Required focused pytest, new skill catalog pytest, narrow changed-file ruff,
  and web type-check passed.
- Broad NGA-02 ruff remains blocked by existing lint debt in the wider phase
  scope.
- Continue NGA-02 with `NGA-F005` only.

## NGA-02 NGA-F005 Handoff Decision

- `NGA-F005` is passing with actor and critic evidence.
- MCP tenant policy now denies by default when no configured allow-list is
  available and records the policy source for inspection.
- Factory-created tool invokers no longer expose or run MCP tools without a
  tenant MCP policy service.
- Denied MCP calls are audited with `output_status="denied"` before returning.
- MCP tool catalog metadata is bounded, tenant-policy scoped, and redacts
  credential-shaped strings.
- Required focused pytest, new MCP policy pytest, narrow changed-file ruff, and
  web type-check passed.
- Broad NGA-02 ruff remains blocked by existing lint debt in the wider phase
  scope.
- Continue NGA-02 with `NGA-F006` only.

## NGA-02 NGA-F006 Handoff Decision

- `NGA-F006` is passing with actor and critic evidence.
- Generated skills now remain proposed and disabled until independent critic
  evidence, eval evidence, and rollback metadata are present.
- `SkillRegistry.save_manifest()` reuses existing status columns and does not
  require a schema migration.
- `skill_create` now produces proposed reviewable SKILL.md output and explicitly
  avoids direct register/enable behavior.
- Required focused pytest, new generated-skill safety pytest, narrow
  changed-file ruff, and web type-check passed.
- Broad NGA-02 ruff remains blocked by existing lint debt in the wider phase
  scope.
- `NGA-02` is passed with a documented broad-ruff caveat. Continue with
  `NGA-03` / `NGA-F007`.

## NGA-03 NGA-F007 Handoff Decision

- `NGA-F007` is passing with actor and critic evidence.
- Memory profiles are explicit: `off` blocks long-term write/recall while
  allowing delete; `basic` allows semantic facts/preferences only; `hybrid`
  allows proposed procedural memory.
- Memory writes now carry memory type, profile, tenant/user/session scope,
  privacy flags, and untrusted-data metadata.
- Memory values are filtered for email/phone PII and prompt-control phrases
  before persistence, search exposure, and long-term recall exposure.
- Runtime markdown memory source deletion is confined to the active tenant/user
  root.
- The memory tool honors profile/type gates, supports inspect, sanitizes stored
  values, and does not echo raw values.
- Required memory/context pytest passed, required safety pytest passed, and
  changed F007 files pass focused ruff.
- Required broad NGA-03 ruff remains blocked by 23 existing lint errors outside
  changed F007 files.
- Continue NGA-03 with `NGA-F008` only. Do not unlock NGA-04 until `NGA-F008`
  and `NGA-F009` also pass, are blocked with named gaps, or are explicitly
  waived.

## NGA-03 NGA-F008 Handoff Decision

- `NGA-F008` is passing with actor and critic evidence.
- Long uploaded documents that require RAG are collected by
  `FileProcessor.process_files()` and handed to `create_session_kb()`.
- `create_session_kb()` calls an injected `create_session_dataset` KB proxy when
  available and records session-file source type, freshness, and
  tenant/user/session scope metadata.
- Missing or unsupported KB proxies fail safe by returning `None`; no live KB
  data, schema migration, or knowledge-service ingestion rewrite was required.
- `ScenarioRetrievalContext.to_formatted_context()` now includes bounded source
  metadata for source type, citation, freshness, dataset, chunk, tenant, user,
  and session.
- Required memory/context pytest passed, required safety pytest passed, and the
  required broad NGA-03 ruff command now passes after mechanical lint cleanup in
  phase validation paths.
- Continue NGA-03 with `NGA-F009` only. Do not unlock NGA-04 until `NGA-F009`
  also passes, is blocked with a named context-budget gap, or is explicitly
  waived.

## NGA-03 NGA-F009 Handoff Decision

- `NGA-F009` is passing with actor and critic evidence.
- Context budget events now include deterministic context packet order and
  compaction details.
- `ContextAssemblerV2.build()` accepts bounded source summaries, recent tool
  result summaries, artifact summaries, and compaction summary.
- Request context keeps existing RAG/current context first, appends bounded
  source/tool/artifact/compaction summaries, then appends the current user query
  through the existing context engine.
- Raw long tool output, full skill instructions, and full artifacts are not
  injected into every turn in the focused test.
- Context cost breakdown now attributes source summaries, tool results,
  artifacts, and compaction summaries separately.
- Required memory/context pytest passed, required safety pytest passed, required
  broad NGA-03 ruff passed, strict harness validation passed, and the NGA-03
  completion gate passed.
- `NGA-03` is passed. Continue with `NGA-04` / `NGA-F010` only.

## NGA-04 NGA-F010 Handoff Decision

- `NGA-F010` is passing with actor and critic evidence.
- The assistant Activity timeline now renders existing backend harness state
  from `processSummary.steps`, `processSummary.tools`, context budget,
  compaction state, retrieved contexts, and generated artifacts.
- Legacy `task_planning` and `working_memory_update` stream events now mirror
  into the active message `processSummary`, so the V2 assistant UI exposes
  plan/review/execute state without relying on the hidden legacy task panel.
- Approval events now create or update visible process-summary tool rows even
  when the approval event arrives before a tool row exists.
- Desktop Activity remains the right-side panel; mobile Activity uses the same
  `ActivityPanel` in a bottom sheet.
- Focused F010 Playwright red/green passed, frontend type-check/lint/build
  passed, required API pytest passed, and rendered desktop/mobile checks passed.
- Full listed e2e remains environment-limited in this checkout because the real
  local e2e stack requires `POSTGRES_PASSWORD` and `REDIS_PASSWORD`; the reused
  lightweight API stub does not implement backend memory/history/playground
  behavior. The new F010 test passed in that full command.
- Continue NGA-04 with `NGA-F011` only. Do not advance to NGA-05 until F011
  passes, is blocked with a precise session UX/backend contract gap, or is
  explicitly waived.

## NGA-04 NGA-F011 Handoff Decision

- `NGA-F011` is passing with actor and critic evidence.
- Restored assistant sessions now count unique artifact IDs across persisted
  artifacts and reconstructed current-run output files.
- A restored one-artifact session now renders `Artifacts 1`, exposes one
  artifact in the Share dialog, and sends `include_artifacts: true` when the
  user creates the share.
- The focused Playwright fixture can preload assistant sessions, history,
  artifacts, and share responses without backend contract changes.
- Desktop and mobile rendered evidence passed with no horizontal overflow.
- Frontend type-check/lint/build passed, and required assistant session/share
  API pytest passed.
- Full listed e2e remains environment-limited in this checkout because the real
  local e2e stack requires `POSTGRES_PASSWORD` and `REDIS_PASSWORD`; the reused
  lightweight API stub does not implement backend memory/history/playground
  behavior. The new F010 and F011 tests passed in that full command.
- `NGA-04` is passed. Continue with `NGA-05` / `NGA-F012`.

## NGA-05 NGA-F012 Terminal Gate Decision

- `NGA-F012` is blocked with actor and critic evidence.
- Assistant safety gates passed through the required pytest set for eval safety,
  guardrails, safe-fetch, safe-fetch call sites, and tool orchestration.
- Assistant integration gates passed for OpenAPI and core isolation; service
  failure-isolation checks skipped locally because docker-compose services were
  not running.
- The exact frontend release command passed, including type-check, lint, build,
  and open-source Playwright route smoke.
- Committed Docker Compose config validates with `.env.example`.
- External release env config and runtime gates are blocked before runtime
  checks by six named missing or placeholder settings:
  `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`,
  `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`,
  `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and
  `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.
- No product code, schema, env file, deployment target, provider credential, or
  production data changed in NGA-05.
- Terminal release remains locked. The next agent or operator must update the
  external env file without exposing values, then rerun `make validate-config`
  and `make validate` with the same `ENV_FILE` path.

## NGA-05 NGA-F012 Continuation Recheck

- The external env file path remains readable.
- `make validate-config` and `make validate` were rerun with the specified
  `ENV_FILE` path and still fail on the same six release settings:
  `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`,
  `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`,
  `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and
  `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.
- Runtime checks remain unproven because the config gate fails first.
- No code or env file changed; the terminal release lock remains in force.

## NGA-05 NGA-F012 Third Recheck

- `make validate-config` and `make validate` were rerun again with the
  specified `ENV_FILE` path and still fail on the same six release settings:
  `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`,
  `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`,
  `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and
  `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.
- Runtime checks remain unproven because the config gate fails first.
- This is now a repeated external-state impasse. The release lock remains in
  force until operator-side env remediation or explicit waiver.

## NGA-05 NGA-F012 User Waiver

- User instructed "那先不管" for the repeated external env release gate.
- `NGA-F012` is waived for harness completion only; this does not make
  production release readiness true.
- Actual release remains gated on passing `make validate-config` and
  `make validate` with the specified external `ENV_FILE` path.
- No code, env file, deployment, migration, provider credential, or production
  data changed.
- Strict harness validation and terminal completion gate passed after waiver
  writeback.
