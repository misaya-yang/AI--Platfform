# NGA-03 / NGA-F007 Independent Critic

Critic: independent fresh-context reviewer for NGA-03 / NGA-F007.
Actor Report Reviewed: docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f007-report.md
Critic Verdict: approved

## Critic Identity

Role: independent harness critic for memory privacy and boundary behavior.

## Review Scope

- Actor report:
  `docs/general_ai_assistant_next_gen/reports/nga-03-memory-rag-and-context-foundation-f007-report.md`
- Code paths:
  - `apps/assistant-service/src/assistant_service/core/memory/memory_manager.py`
  - `apps/assistant-service/src/assistant_service/core/runtime/memory/source_store.py`
  - `apps/assistant-service/src/assistant_service/core/tools/memory_tool.py`
  - `tests/services/assistant/test_memory_manager.py`
- Harness writeback requirements for NGA-F007.

## Findings

- `off`, `basic`, and `hybrid` profiles are explicit and covered by focused
  tests.
- Long-term writes and recall fail closed under `off`; explicit delete remains
  available so users can purge existing durable memory after disabling recall.
- `basic` allows semantic long-term memory but blocks procedural writes.
- `hybrid` permits procedural memory only as proposed metadata, which is
  consistent with NGA-F006 generated-skill safety.
- Tenant/user/session scoping remains explicit in database calls and is now
  copied into memory metadata.
- Memory values are treated as untrusted data and filtered for PII and
  prompt-control strings before persistence or recall exposure.
- `MemorySourceStore.delete_source()` is confined to the active tenant/user
  root and refuses path traversal or cross-tenant deletion.
- `update_user_memory` no longer echoes raw stored values.
- The exact NGA-03 ruff command still fails because of 23 pre-existing
  wider-scope lint issues in file-processing, compressor, RAG, and safe-fetch
  tests. Changed F007 files pass focused ruff.

## Critic Verdict

Pass with documented phase-partial caveat. NGA-F007 satisfies the memory profile,
tenant scope, PII filtering, explicit recall/delete, inspect, and
prompt-injection boundary requirements without migrations, live data, secrets,
frontend changes, or deployments. NGA-03 must continue with NGA-F008 and
NGA-F009 before the phase can pass.
