# NGA-03 Memory RAG and Context Foundation Plan

## Active Slice

- Phase: `NGA-03 Memory RAG and Context Foundation`
- Feature: `NGA-F007`
- Scope: procedural, situational, and semantic memory boundaries only.

## Observed Context

- `MemoryManager` already has working, session, and long-term layers with tenant,
  user, and session parameters, but it does not yet enforce `off`, `basic`, or
  `hybrid` profiles.
- `MemorySourceStore` persists markdown under per-tenant/per-user roots and can
  inspect those files, but deletion is not yet explicit.
- `memory_tool.py` can set and delete user memory, but it does not expose profile
  policy or avoid echoing stored values.
- `ContextEngine` can include long-term memory, but `NGA-F007` will limit changes
  to memory boundaries and leave broader context packet work to `NGA-F009`.

## Implementation Plan

1. Add focused red tests in `tests/services/assistant/test_memory_manager.py` for:
   - `off`, `basic`, and `hybrid` memory profile policy.
   - long-term tenant/user/session DB scoping.
   - PII redaction before memory persistence.
   - prompt-injection content treated as untrusted memory data.
   - explicit long-term recall and delete behavior.
   - source-store delete confined to the active tenant/user root.
2. Implement the smallest policy layer in
   `apps/assistant-service/src/assistant_service/core/memory/memory_manager.py`.
3. Add bounded delete support in
   `apps/assistant-service/src/assistant_service/core/runtime/memory/source_store.py`.
4. Update `apps/assistant-service/src/assistant_service/core/tools/memory_tool.py`
   so memory writes honor profile/type policy and do not echo raw values.
5. Run the required NGA-03 validation commands plus red/green focused evidence.
6. Record actor evidence, independent critic evidence, minimal-change scope, and
   `NGA-F007` oracle writeback without advancing past `NGA-03`.

## Non-Goals

- No database migration.
- No production KB or live memory data access.
- No frontend UI changes.
- No RAG/session-KB implementation beyond what is needed to keep the memory
  source-store boundary explicit.

## NGA-F008 Active Slice

- Phase: `NGA-03 Memory RAG and Context Foundation`
- Feature: `NGA-F008`
- Scope: scoped RAG sources for KB/session files/generated artifacts/web/MCP
  metadata, session-level temporary KB handoff for long uploaded files, and
  source-aware context formatting.

## NGA-F008 Observed Context

- `FileProcessor._process_document` already flags long documents with
  `requires_rag`, but `process_files` does not call `create_session_kb`.
- `FileProcessor.create_session_kb` is a placeholder that returns `None` even
  when a knowledge-service proxy is injected.
- `ScenarioRetrievalContext.to_formatted_context` includes source names and
  scores, but not source type, citation, freshness, dataset, or tenant/session
  scope metadata.

## NGA-F008 Implementation Plan

1. Add focused red tests in `tests/services/assistant/test_context_engine.py`
   covering:
   - session KB creation for long uploaded files with tenant/user/session
     metadata passed to the injected KB proxy;
   - formatted RAG context that preserves source type, citation, freshness,
     dataset ID, and tenant/session scope.
2. Wire long uploaded documents from `process_files` into `create_session_kb`
   and attach `ProcessedFiles.session_kb_id` when the proxy creates one.
3. Implement `create_session_kb` as a narrow adapter over an injected
   session-dataset capable KB proxy, with a safe `None` fallback when the proxy
   is unavailable or unsupported.
4. Enrich scenario retrieval formatting with bounded source metadata already
   present on retrieval results.
5. Run focused fail-before/pass-after checks and the NGA-03 validation commands.
6. Record actor evidence, independent critic evidence, minimal-change notes, and
   `NGA-F008` oracle writeback without advancing to `NGA-04`.

## NGA-F008 Non-Goals

- No schema migration.
- No production KB data, environment, or deployment access.
- No frontend changes.
- No broad knowledge-service ingestion rewrite.

## NGA-F009 Active Slice

- Phase: `NGA-03 Memory RAG and Context Foundation`
- Feature: `NGA-F009`
- Scope: context packet order, budget telemetry, compaction evidence, deferred
  capability metadata, scoped memory/source summaries, and recent
  tool/artifact summaries.

## NGA-F009 Observed Context

- `ContextAssemblerV2.build()` already returns messages, a budget event, and a
  cost detail map, but it does not accept source summaries, tool-result
  summaries, artifact summaries, or compaction summaries as structured context
  packet inputs.
- `ContextAssemblyPlan.to_budget_event()` records budgets and whether
  compaction happened, but not packet order or compaction details.
- `ContextCostBreakdown.analyze()` accounts for system, messages, tools, files,
  skills, and memory snippets, but not RAG source summaries, recent tool
  results, artifact summaries, or compaction summaries.

## NGA-F009 Implementation Plan

1. Add focused red tests in `tests/services/assistant/test_context_engine.py`
   covering:
   - bounded source/tool/artifact/compaction summaries in request context;
   - explicit context packet order in the budget event;
   - compaction telemetry when history is trimmed;
   - cost contributors for source summaries, tool results, artifacts, and
     compaction.
2. Extend `ContextAssemblerV2.build()` with optional structured summary inputs
   and append only bounded summaries to the current request context.
3. Extend `ContextCostBreakdown.analyze()` to attribute the new summary
   categories without serializing raw full outputs.
4. Extend `ContextAssemblyPlan.to_budget_event()` with packet-order and
   compaction telemetry.
5. Run focused fail-before/pass-after checks and the NGA-03 validation commands.
6. Record actor evidence, independent critic evidence, minimal-change notes, and
   `NGA-F009` oracle writeback; mark NGA-03 passed only if all phase gates pass.

## NGA-F009 Non-Goals

- No frontend changes.
- No schema migration.
- No live KB data, provider credentials, or production deployment.
- No replacement of the canonical streaming-first loop.
