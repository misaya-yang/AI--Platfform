# NGA-03 / NGA-F008 Independent Critic

Critic: independent fresh-context reviewer for NGA-03 / NGA-F008.
Actor Report Reviewed: docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f008-report.md
Critic Verdict: approved

## Critic Identity

Role: independent harness critic for RAG source scope, session-file handling,
and citation metadata behavior.

## Review Scope

- Actor report:
  `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f008-report.md`
- Code paths:
  - `apps/assistant-service/src/assistant_service/core/files/file_processor.py`
  - `apps/assistant-service/src/assistant_service/core/rag/scenario_aware_retriever.py`
  - `tests/services/assistant/test_context_engine.py`
- Harness writeback requirements for NGA-F008.

## Findings

- Long uploaded files now have a tested session-KB handoff through
  `FileProcessor.process_files()` and `create_session_kb()`.
- The KB proxy call carries document paths plus source type, freshness, and
  tenant/user/session scope metadata.
- Missing or unsupported KB proxies fail safe by returning `None`; no live
  private KB data or production knowledge-service internals are required for the
  proof.
- Formatted RAG context now preserves source type, citation, freshness,
  dataset ID, chunk ID, and tenant/user/session scope when retrieval metadata
  provides them.
- Metadata values are bounded before inclusion in model context, reducing
  over-retrieval and prompt-bloat risk.
- The focused red tests failed for the exact missing behaviors before the patch
  and passed after implementation.
- Required NGA-03 pytest and broad phase ruff now pass after mechanical lint
  cleanup inside the phase validation paths.

## Critic Verdict

Pass. NGA-F008 satisfies the session-file RAG handoff and source-aware citation
metadata requirements without migrations, deployments, live data, secrets,
frontend changes, or a knowledge-service ingestion rewrite. NGA-03 must continue
with NGA-F009 before the phase can pass.
