# Phase 4 — Remaining `src.*` Imports in `apps/assistant-service/`

**Generated:** end of phase 4.7
**Count:** 6 sites across **1 file** (`main.py` composition root only)
**Core tree (`core/`) status:** `rg "from src\." core/` → **empty** (locked in by `tests/integration/test_assistant_core_isolation.py`)

After Phase 4, the microservice is one file away from total `src.*` freedom. Every surviving `src.*` import lives in `apps/assistant-service/src/assistant_service/main.py` — the composition root. The plan from the beginning was to let this file keep composing the gateway's concrete kernels (DB, sessions, KB-proxy, metrics, storage) via direct imports while everything downstream talks through Protocols.

## Phase 4 exit-gate checklist

| Requirement | Status |
|---|---|
| `rg "from src\." apps/assistant-service/` → only matches inside `main.py` | ✅ 6 sites, all in main.py |
| `rg "from src\." apps/assistant-service/src/assistant_service/core/assistant_service.py` → empty | ✅ empty |
| Dockerfile untouched (Phase 5's job) | ✅ no diff |
| All 7 sub-commits green on broad baseline (306+2 passed / 8 skipped / 0 failed) | ✅ verified between every commit |
| All 7 sub-commits green on `make test-isolation` | ✅ verified between every commit |
| All 7 sub-commits green on OpenAPI diff (empty) | ✅ verified between every commit |
| Bucket B defaults use Null Object, never `None` | ✅ NoOpUsageRecorder / NoOpRealtimeMetrics / NoOpArtifactStorage / NoOpFileStorage |
| 4.4 dead-code verification | ✅ `rg "KnowledgeService\("` empty before rewrite |
| 4.1 dedup before adding | ✅ 0 new Protocols; 4 aligned in place; 4 NoOps added |

## The 6 remaining imports (all in main.py)

Everything here is intentional composition-root wiring. `main.py` fetches the gateway's concrete kernels from `src/` and injects them as Protocol-typed parameters into `AssistantService(...)`. Any downstream code only sees the Protocol.

| line | import | why it's here |
|------|--------|---------------|
| 40  | `from src.persistence.database import DatabaseStorage` | DB kernel construction at lifespan-startup. 264KB file; LOC-gate blocked promotion in Phase 3.4. |
| 117 | `from src.services.knowledge.kb_proxy_client import KBProxyClient` | HTTP proxy client to the KB service. Candidate for promotion to `ai_gateway_core.knowledge` in a future phase if and when the gateway also switches to consuming the KB service over HTTP (it still runs some KB bits in-process). |
| 136 | `from src.services.session.database_session_manager import DatabaseSessionManager` | Chat-session DB manager. Stays in `src/` for now per revised 3.4 plan. |
| 188 | `from src.services.metrics.realtime_metrics import get_realtime_metrics` | Realtime-metrics factory (Redis/Prometheus backed). 560 LOC; LOC-gate blocked promotion in Phase 3.5. |
| 189 | `from src.services.metrics.usage_recorder import get_usage_recorder` | Per-request usage recorder (DB-backed). 1657 LOC; LOC-gate blocked promotion in Phase 3.5. |
| 190 | `from src.services.storage import get_artifact_storage, get_file_storage` | File / artifact storage factories. 662 + 1371 LOC transitive; LOC-gate blocked promotion in Phase 3.5. |

## What Phase 5 will do

1. **Composition-root isolation.** Provide the last six concretes to `main.py` via a different surface than direct `src/` imports. Options:
   - A thin `apps/assistant-service/src/assistant_service/bootstrap.py` module that wraps the imports; Phase 5 decides if that's a meaningful boundary or just the same problem with an extra hop.
   - Runtime plugin loading via entry points (PEP 517).
   - Duplicate the minimal concrete implementations inside the assistant tree (trades shared-code benefit for true isolation).
   - Accept that a microservice extracted from a shared monorepo will always retain a composition-root coupling and adopt a documented allowlist instead of zero-tolerance.
2. **Dockerfile rewrite.** Remove `COPY src/ ./src/` and `PYTHONPATH="...src..."`. Build context becomes `apps/assistant-service/` + `packages/ai-gateway-core/` only. Image size should drop measurably.
3. **Verification.** The `test_main_py_is_only_src_import_site` guard from 4.6 must be updated (or removed) once Phase 5 eliminates those last 6 imports.

The Dockerfile stays untouched in Phase 4 exactly so any Phase-5 regression is instantly attributable to that commit alone.

## Bucket status recap

| Bucket | Phase 3 disposition | Phase 4 disposition |
|---|---|---|
| **A** (DB / Session / KB-proxy kernels in main.py, 3 imports) | deferred | still in main.py; core/ side uses Protocols via TYPE_CHECKING where annotated |
| **B** (metrics + storage factories, 5 sites in `core/assistant_service.py`) | deferred | **evicted via Protocol + DI + NoOp defaults**. Concrete factories moved into main.py wiring (lines 188-190). |
| **C** (`src.services.knowledge.*`, 10 sites in 6 files) | deferred | **evicted**: TYPE_CHECKING swaps (5 sites → `KnowledgeClientLike`); runtime swap (1 site, dead-code-verified → `KnowledgeClientLike`); inline (1 site, `import_pymupdf`); promotions (2 sites, `ISLAMIC_SYNONYMS` + `get_authority_order`); annotation-only (1 site, `DashScopeVLMService` → `Any`). |

## New Protocol surface delta vs. Phase 2

Phase 2 shipped 8 Protocols with speculative method signatures. Phase 4.1 aligned 4 of them with concrete-impl reality (renames, not additions). Phase 4.5 added two small pure-data modules to `ai_gateway_core.knowledge`. No new Protocols were created. Everything was done by extending what existed.

| Protocol | Phase 2 → Phase 4 |
|---|---|
| `UsageRecorderLike` | `record(**)` → `record_usage(**)` |
| `RealtimeMetricsLike` | `incr` / `gauge` → `record_token_usage(input, output)` |
| `FileStorageLike` | `put/get/delete/presigned_url` → `download_file(path)` + `config: Any` |
| `ArtifactStorageLike` | `save/load/url_for` → `create_artifact(**)`, `get_presigned_download_url(artifact, expiry)` |
| `UserContextLike` | unchanged |
| `DatabaseStorageLike` | unchanged |
| `SessionManagerLike` | unchanged |
| `KnowledgeClientLike` | unchanged |

Plus 4 NoOp reference implementations (`NoOpUsageRecorder`, `NoOpRealtimeMetrics`, `NoOpFileStorage`, `NoOpArtifactStorage`) and 2 pure-data modules (`ISLAMIC_SYNONYMS` in `ai_gateway_core.knowledge._synonyms`, `get_authority_order` + `AUTHORITY_ORDER` in `ai_gateway_core.knowledge._authority`).

## Phase 4 import-count arithmetic

```
start of Phase 4 (post-3.6):    16 src.* imports
  bucket A composition root:     3
  bucket B runtime factories:    5
  bucket C replace knowledge:    8

after 4.2 (Bucket B eviction):  10 src.* imports (main.py gains 4 for composition)
after 4.3 (TYPE_CHECKING swaps): 10 (annotation-only, no runtime change)
after 4.4 (runtime KB swap):     9
after 4.5 (fringe handling):     6
after 4.6 (guard test added):    6 (+ invariant now enforced)
after 4.7 (this report):         6 (all in main.py)

core/ tree:                       0
```

Phase 4 goal — core/ has zero `src.*`, main.py holds the composition residue — **achieved**.
