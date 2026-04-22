# Phase 3.5 — Remaining `src.*` Imports in `apps/assistant-service/`

**Generated:** end of phase 3.5
**Count:** 16 sites across 10 files
**Gate:** `rg "from src\." apps/assistant-service/`

After 3.1 → 3.5, the microservice is physically extracted, the `sys.path`
hack is gone, shared primitives + contract Protocols live in
`ai-gateway-core`, and `UserContext` is a cross-service type. These
remaining 16 imports fall into three buckets and are intentionally
deferred.

---

## Bucket A — `main.py` composition root (3 sites, won't remove in Phase 3)

The service's entrypoint wires concrete infrastructure (DB, KB client,
session manager) and passes instances into `AssistantService`. Per the
revised 3.4/3.5 plan, DB/session concretes stay in `src/`; main.py is
the one place allowed to know where they live.

| site | import | reason to defer |
|------|--------|-----------------|
| `main.py:40`  | `src.persistence.database.DatabaseStorage` | 264KB kernel, >500-LOC gate tripped |
| `main.py:117` | `src.services.knowledge.kb_proxy_client.KBProxyClient` | HTTP client; Phase 4 will decide whether to promote or keep |
| `main.py:136` | `src.services.session.database_session_manager.DatabaseSessionManager` | DB-backed session store, stays per revised 3.4 |

**Cleanup path (future phase, not Phase 3):** move each concrete into
`apps/assistant-service/src/assistant_service/` proper, or duplicate
into `ai-gateway-core` once the LOC trade-off is accepted. Not worth
doing during the isolation migration.

---

## Bucket B — runtime metrics/storage factories (5 imports on 4 lines in `assistant_service.py`)

Top-of-file imports of factory functions that are invoked inside
methods at runtime (4 call sites total: `self.artifact_storage =
get_artifact_storage()` etc.). Per the 3.5 LOC-gate report:

| module | LOC | gate |
|--------|----:|------|
| `src/services/metrics/realtime_metrics.py` | 560  | ⚠️ over |
| `src/services/metrics/usage_recorder.py`   | 1657 | ⛔   |
| `src/services/storage/file_storage.py`     | 662  | ⚠️ over |
| `src/services/storage/image_storage.py`    | 1371 | ⛔   |

| site | import | reason to defer |
|------|--------|-----------------|
| `assistant_service.py:49` | `src.services.metrics.realtime_metrics.get_realtime_metrics` | LOC-gate trip; 4 gateway-side callers |
| `assistant_service.py:50` | `src.services.metrics.usage_recorder.get_usage_recorder` | LOC-gate trip; 1 gateway-side caller |
| `assistant_service.py:51` | `src.services.storage.get_artifact_storage, get_file_storage` | LOC-gate trip (file_storage.py 662 LOC; image_storage.py 1371 LOC transitively) |

**Cleanup path (future phase):** inject all four into
`AssistantService.__init__` from `main.py`. Then these runtime imports
disappear from `core/` and live only at the composition root (joining
bucket A). Not done in Phase 3 to keep the constructor signature stable
and avoid widening 3.5 beyond the LOC-gate scope.

---

## Bucket C — `replace` bucket, Phase 4 scope (8 sites)

All `src.services.knowledge.*` imports. The migration plan's §3 step 2
treats these as HTTP-client replacements — assistant must talk to KB
service via `KBProxyClient` over HTTP, not in-process. Phase 4 will
design the replacement for each symbol individually.

| site | import | disposition (Phase 4 decides) |
|------|--------|-------------------------------|
| `core/tools/builtin_tools.py:30`        | `KnowledgeService`         | HTTP proxy |
| `core/content/streaming_writer.py:42`   | `KnowledgeService`         | HTTP proxy |
| `core/agent/agent_loop.py:134`          | `KnowledgeService`         | HTTP proxy |
| `core/rag/scenario_aware_retriever.py:32` | `KnowledgeService`       | HTTP proxy |
| `core/files/file_processor.py:42`       | `KnowledgeService`         | HTTP proxy |
| `core/files/file_processor.py:43`       | `DashScopeVLMService`      | HTTP proxy (VLM is KB-side capability) |
| `core/files/pdf_converter.py:22`        | `import_pymupdf` helper    | inline — trivial 3-line helper |
| `core/assistant_service.py:48`          | `KnowledgeService`         | HTTP proxy |
| `core/assistant_service.py:4437`        | `get_authority_order`      | promote to `ai_gateway_core` if data-only, else HTTP |
| `core/quality/domain_policies.py:8`     | `ISLAMIC_SYNONYMS` constant | promote to `ai_gateway_core` (pure data) |

Most of Bucket C (7 of 10) are inside `if TYPE_CHECKING:` blocks — they
already contribute zero runtime coupling; only the annotations refer to
the concrete types. The runtime sites are the three at the bottom of
the table.

---

## Summary

Phase 3 acceptance criterion `rg "from src\." apps/assistant-service/ → 0`
is **not reachable by Phase 3 alone** without promoting kernel
infrastructure or rewriting the KB call boundary, both of which are
out of scope per the revised plan and the Phase 1 inventory classification.

Phase 3 delivered:

- Physical move of 72K LOC / 252 files
- sys.path hack removed
- Shared primitives + 7 Protocols in `ai-gateway-core`
- `UserContext` is a cross-service concrete type
- 14 TYPE_CHECKING annotation sites wired to Protocols

Phase 3.6 (next) rewrites the **gateway-side** imports of the Phase-2
shims (`src.core.observability.logging` / `src.core.exceptions` /
`src.models.enums`) so those shim files can be deleted.

Phase 4 (future) will tackle Bucket C (knowledge HTTP client rewrite).
Phase 5+ (future) will revisit Buckets A/B if independent deployment
requirements force elimination of kernel coupling.
