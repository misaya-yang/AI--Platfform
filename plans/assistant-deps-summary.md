# Assistant Service — Dependency Inventory Summary (Phase 1)

**Generated:** 2026-04-22 (by `scripts/analyze_assistant_deps.py`)
**Source:** `src/services/assistant/` (252 `.py` files scanned)
**Inventory JSON:** `plans/assistant-deps-inventory.json`

---

## Headline Counts

| Bucket    | Distinct (target, symbol) entries | % |
|-----------|-----------------------------------|---|
| shared    | 3  | 20.0% |
| contract  | 7  | 46.7% |
| move      | 0  | 0.0%  |
| replace   | 5  | 33.3% |
| review    | 0  | 0.0%  |
| **Total** | **15** | 100% |

- **90** total external-import call sites (sum of `call_sites_count` across all entries).
- **14** distinct `target_module` values (one module, `src.services.storage`, contributes two entries via two different symbols).

One dominant dependency: `src.core.observability.logging.get_logger` alone accounts for **56 of the 90** call sites (62%). Once that is moved to `ai-gateway-core`, the bulk of the import-rewrite work is done.

---

## shared (3 entries)

Pure utilities / exception classes / enum types. These go straight into `packages/ai-gateway-core/`.

- **`src.core.observability.logging`** — `get_logger` (56 call sites)
  Examples: `agent/agent_loop.py`, `assistant_service.py`, `artifacts.py`
- **`src.core.exceptions`** — `PermissionDeniedError` (1 call site)
  Example: `assistant_service.py`
- **`src.models.enums`** — `StreamEventType` (4 call sites)
  Examples: `agent/agent_loop.py`, `agent/agui_protocol.py`, `agent/artifact_persister.py`

---

## contract (7 entries)

Interface is shared; implementation differs per service. `Protocol`/`ABC` goes in `ai-gateway-core`; concrete impls stay per-service.

- **`src.core.auth.user_resolver`** — `UserContext` (9 call sites)
  Examples: `agent/agent_loop.py`, `assistant_service.py`, `artifacts.py`
- **`src.persistence.database`** — `DatabaseStorage` (4 call sites)
  Examples: `memory_service.py`, `quiz/exam_service.py`, `quiz/quiz_service.py`
- **`src.services.session.database_session_manager`** — `DatabaseSessionManager` (1 call site)
  Example: `assistant_service.py`
  Both services need session state; keep in `ai-gateway-core/session/` as a `Protocol` with concrete impls per service.
- **`src.services.metrics.realtime_metrics`** — `get_realtime_metrics` (1 call site, `assistant_service.py`)
- **`src.services.metrics.usage_recorder`** — `get_usage_recorder` (1 call site, `assistant_service.py`)
- **`src.services.storage`** — `get_artifact_storage` (1 call site, `assistant_service.py`)
- **`src.services.storage`** — `get_file_storage` (1 call site, `assistant_service.py`)

---

## move (0 entries)

Empty. The rule matched `src.openclaw.*`, which is not imported anywhere. The only openclaw copy lives at `src/services/assistant/openclaw/` already; Phase 3 step 4 ("de-duplicate openclaw") is a no-op.

---

## replace (5 entries)

In-process calls into `src.services.knowledge.*` that must become HTTP calls via `KBProxyClient` (or die — see per-item notes).

- **`src.services.knowledge.knowledge_service`** — `KnowledgeService` (6 call sites)
  Examples: `agent/agent_loop.py`, `assistant_service.py`, `content/streaming_writer.py`
  The canonical case for `AssistantProxyClient` → KB-service HTTP hop.
- **`src.services.knowledge.vlm_service`** — `DashScopeVLMService` (1 call site, `files/file_processor.py`)
  VLM should be a KB-service capability; assistant hits it over HTTP rather than instantiating a DashScope client in-process.
- **`src.services.knowledge.islamic_metadata`** — `get_authority_order` (1 call site, `assistant_service.py`)
  Small pure-function utility; candidate for promotion to `shared` if it's data-only. Operator to review during Phase 2 — if it reads from a DB/file, keep as `replace`.
- **`src.services.knowledge.constants`** — `ISLAMIC_SYNONYMS` (1 call site, `quality/domain_policies.py`)
  Pure data constant. Likely promotable to `shared` in ai-gateway-core if the gateway also uses it; otherwise duplicate into the assistant tree.
- **`src.services.knowledge.common`** — `import_pymupdf` (1 call site, `files/pdf_converter.py`)
  Lazy-import helper for `pymupdf`. Trivially inline-able inside the assistant (three lines of `try/except ImportError`); no reason to route through KB service.

---

## review (0 entries)

Empty. The four previously-unclassified entries (metrics + storage) are now classified as `contract`. The three previously-broken `TYPE_CHECKING` imports were fixed in place to point at their real targets — two collapsed into internal deps (no longer in the inventory), one is now correctly counted under `src.persistence.database` in the `contract` bucket.

---

## Notes for the Operator

1. **`openclaw` de-dup is a no-op.** There is only one copy (`src/services/assistant/openclaw/`); `src/openclaw/` does not exist. Phase 3 step 4 can be skipped.
2. **`get_logger` dominates.** 56 of 90 call sites (~62%) are a single import. Moving that one symbol to `ai-gateway-core.logging` closes the majority of the work.
3. **Three `TYPE_CHECKING` imports were silently broken** (wrong relative-dot counts pointing at non-existent modules). Fixed in this phase. Static type-checkers were losing type info for three symbols before this.
4. **No gateway → assistant reverse imports.** Confirmed separately; this inventory only walks forward from the assistant module.
