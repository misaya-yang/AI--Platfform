# NGA-03 Memory RAG and Context Foundation Report

## Status

Passed. NGA-F007, NGA-F008, and NGA-F009 are passing with actor and critic
evidence. NGA-03 unlocks NGA-04.

## NGA-F007 Result

Procedural, situational, and semantic memory now have explicit backend
boundaries:

- `off`, `basic`, and `hybrid` memory profiles are represented in code.
- `off` blocks durable long-term writes and recall while keeping explicit delete
  available.
- `basic` allows semantic long-term facts/preferences and blocks procedural
  memory.
- `hybrid` allows procedural memory only as proposed metadata.
- Memory metadata records type, profile, tenant/user/session scope, privacy
  filter flags, and `untrusted_memory_data` trust state.
- Memory values are sanitized for email/phone PII and prompt-control strings
  before storage, search exposure, and long-term recall exposure.
- Runtime markdown memory sources can be deleted only inside the active
  tenant/user root.
- The memory tool honors profile/type policy and avoids echoing raw stored
  values.

## Evidence

- Plan: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-plan.md`
- F007 actor report: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f007-report.md`
- F007 independent critic: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f007-critic.md`
- F008 actor report: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f008-report.md`
- F008 independent critic: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f008-critic.md`
- F009 actor report: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f009-report.md`
- F009 independent critic: `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f009-critic.md`

## NGA-F008 Result

RAG/file source handling now records explicit session-file and citation
boundaries:

- Long uploaded documents marked for RAG are collected during file processing.
- `create_session_kb()` creates a session dataset through an injected
  `create_session_dataset` KB proxy when available.
- Session KB metadata includes `source_type: session_file`, freshness, and
  tenant/user/session scope.
- Missing or unsupported KB proxies fail safe by returning `None` while keeping
  `requires_rag` visible.
- Formatted scenario retrieval context includes bounded source type, citation,
  freshness, dataset ID, chunk ID, and tenant/user/session scope metadata.
- Focused tests cover session-KB handoff and source-aware formatted RAG context.

## NGA-F009 Result

Context assembly now has explicit packet order and budget telemetry:

- Budget events include deterministic context packet order and compaction
  details.
- `ContextAssemblerV2.build()` accepts optional bounded source summaries,
  tool-result summaries, artifact summaries, and compaction summary.
- Request context preserves existing RAG/current context first, then source
  summaries, recent tool results, recent artifacts, compaction summary, and the
  current user query.
- Summary items are capped and normalized so full skill instructions, raw tool
  output, and full artifacts are not stuffed into every turn.
- Context cost detail attributes source summaries, tool results, artifacts, and
  compaction summaries as separate contributor categories.

## Validation

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py -k "MemoryManagerProfiles or MemoryManagerPrivacyBoundaries or MemorySourceStoreBoundaries"` before implementation | Failed as intended at collection: missing memory profile API. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py -k "MemoryManagerProfiles or MemoryManagerPrivacyBoundaries or MemorySourceStoreBoundaries or MemoryToolBoundaries"` | Passed: 11 passed, 58 deselected, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_context_engine.py::TestRAGSourceScope` before implementation | Failed as intended: `session_kb_id` was `None` and formatted RAG context omitted source metadata. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_context_engine.py::TestRAGSourceScope` | Passed: 2 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py` | Passed: 185 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` | Passed: 70 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/memory/memory_manager.py apps/assistant-service/src/assistant_service/core/runtime/memory/source_store.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py` | Passed: all checks passed. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py` after F008 lint cleanup | Passed: 187 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` after F008 lint cleanup | Passed: 70 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/memory apps/assistant-service/src/assistant_service/core/runtime/memory apps/assistant-service/src/assistant_service/core/runtime/context apps/assistant-service/src/assistant_service/core/rag apps/assistant-service/src/assistant_service/core/files/file_processor.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` | Passed: all checks passed after mechanical lint cleanup inside NGA-03 validation paths. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_context_engine.py::TestContextPacketBudget` before implementation | Failed as intended: `ContextAssemblerV2.build()` did not accept `source_summaries`. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_context_engine.py::TestContextPacketBudget` | Passed: 1 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py` after F009 changes | Passed: 188 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` after F009 changes | Passed: 70 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/memory apps/assistant-service/src/assistant_service/core/runtime/memory apps/assistant-service/src/assistant_service/core/runtime/context apps/assistant-service/src/assistant_service/core/rag apps/assistant-service/src/assistant_service/core/files/file_processor.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` after F009 changes | Passed: all checks passed. |
| `python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json`, `loop-state.json`, and `loop-contract.json` | Passed after final writeback. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed after final writeback: quality score 100. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --completion-gate --phase NGA-03 --quality-score` | Passed after final writeback: quality score 100. |
| `git diff --check` | Passed after final writeback. |

## Browser Check

Not applicable for NGA-F007, NGA-F008, or NGA-F009. No frontend file was
changed.

## Minimal-Change Scope

- F007 edited only the assistant-service memory manager, runtime memory source
  store, memory tool, focused assistant memory tests, and harness artifacts.
- F008 behavioral edits were limited to the existing assistant-service file
  processor, scenario-aware retriever, focused context tests, and harness
  artifacts. Mechanical lint cleanup stayed inside NGA-03 validation paths.
- F009 behavioral edits were limited to existing runtime context assembly,
  context cost attribution, RAG context budget events, focused context tests,
  and harness artifacts.
- Did not edit frontend files, database migrations, env files, provider
  credentials, deployments, production KB data, knowledge-service ingestion
  internals, or NGA-01/NGA-02 contracts.

## Current Decision

NGA-F007, NGA-F008, and NGA-F009 are passing. NGA-03 is passed and NGA-04 is
unlocked. Continue with NGA-04 / NGA-F010 only.
