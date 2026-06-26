# NGA-03 / NGA-F007 Actor Report

Status: passing

## Status

NGA-F007 is passing for this iteration. NGA-03 remains partial because
NGA-F008 and NGA-F009 are still pending.

## Plan Followed

- Selected the active loop item: NGA-03 / NGA-F007 only.
- Read the required harness files, the active phase contract, source packet,
  and the phase `PRIMARY_CONTEXT`.
- Wrote the NGA-03 plan artifact before implementation edits.
- Added red tests for memory profiles, tenant/user/session scope, PII filtering,
  prompt-injection boundaries, explicit recall/delete, source-store delete
  confinement, and memory-tool profile handling.
- Implemented the smallest memory-boundary contract inside the existing
  assistant-service memory manager, runtime source store, memory tool, and
  focused tests.

## Files Changed

- `apps/assistant-service/src/assistant_service/core/memory/memory_manager.py`
- `apps/assistant-service/src/assistant_service/core/runtime/memory/source_store.py`
- `apps/assistant-service/src/assistant_service/core/tools/memory_tool.py`
- `tests/services/assistant/test_memory_manager.py`
- Harness files under `docs/general_ai_assistant_next_gen/**`

## Implementation Notes

- `MemoryProfile` now defines `off`, `basic`, and `hybrid`.
- `MemoryType` now defines `procedural`, `situational`, and `semantic`.
- `MemoryManager` defaults to `hybrid` for backward compatibility and can be
  constructed with a stricter profile.
- `off` blocks long-term writes and recall while still allowing explicit
  deletion.
- `basic` allows long-term semantic facts and preferences only.
- `hybrid` allows procedural memory, but procedural entries are marked
  `review_status: proposed` by default.
- Memory writes add explicit `memory_type`, `memory_profile`, tenant/user/session
  scope, privacy flags, and `trust: untrusted_memory_data` metadata.
- Memory values are filtered for email/phone PII and common prompt-control text
  before storage, search-result exposure, and long-term recall.
- `MemoryManager.inspect_memory_policy()` exposes profile, storage, retrieval,
  inspect, delete, and privacy boundaries without exposing memory values.
- `MemorySourceStore.delete_source()` deletes only markdown files inside the
  active tenant/user memory root.
- `update_user_memory` now supports profile/type gates and an `inspect` action,
  blocks long-term writes under `off`, blocks procedural writes under `basic`,
  sanitizes stored values, and no longer echoes raw memory values.

## Validation Evidence

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py -k "MemoryManagerProfiles or MemoryManagerPrivacyBoundaries or MemorySourceStoreBoundaries"` before implementation | Failed as intended at collection: missing `MemoryPolicyError`/profile API. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py -k "MemoryManagerProfiles or MemoryManagerPrivacyBoundaries or MemorySourceStoreBoundaries or MemoryToolBoundaries"` | Passed: 11 passed, 58 deselected, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py` | Passed: 185 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` | Passed: 70 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/memory/memory_manager.py apps/assistant-service/src/assistant_service/core/runtime/memory/source_store.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py` | Passed: all checks passed. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/memory apps/assistant-service/src/assistant_service/core/runtime/memory apps/assistant-service/src/assistant_service/core/runtime/context apps/assistant-service/src/assistant_service/core/rag apps/assistant-service/src/assistant_service/core/files/file_processor.py apps/assistant-service/src/assistant_service/core/tools/memory_tool.py tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py tests/services/assistant/test_preference_extractor.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py` | Failed: 23 existing wider-scope lint errors outside changed F007 files. Changed F007 files pass focused ruff. |
| `python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json`, `loop-state.json`, and `loop-contract.json` | Passed after final writeback. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed after final writeback: quality score 100. |
| `git diff --check` | Passed after final writeback. |

## Browser Check

Not applicable. NGA-F007 targets backend memory boundaries and does not require
browser validation.

## Minimal Change Scope

The code change is limited to existing assistant-service memory manager,
runtime memory source store, memory tool, and focused tests. No dependency,
schema migration, deployment, env file, provider credential, production KB data,
frontend file, or NGA-01/NGA-02 upstream contract was changed.

## Residual Risk

This slice defines backend memory boundaries and tests them with fake stores.
User-facing controls for inspecting/deleting memory remain downstream NGA-04
UX work. Broader context-packet treatment of memory snippets is still assigned
to NGA-F009.

## Decision

NGA-F007 can move to passing. Continue NGA-03 with NGA-F008 only; do not unlock
NGA-04 until NGA-F008 and NGA-F009 also pass, are explicitly waived, or are
blocked with named gaps.

## Feature Oracle Updates

- `NGA-F007` status: passing.
- Evidence: this actor report, the separate NGA-F007 critic artifact, and the
  partial NGA-03 phase report.
