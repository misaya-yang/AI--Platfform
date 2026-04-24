# Phase K5c — Migration Plan (KB ingestion + Confluence + HMAC)

> Roadmap: `plans/Roadmap-Post-5a-Extraction-2026-04-23.md` Phase K5c.
> Polaris target this phase: **item #6-KB (网络边界, KB)** — install HMAC middleware on
> `knowledge-service`. Secondary: ensure the gateway process no longer performs any
> in-process KB ingestion (embedding / qdrant upsert / confluence polling).

## Starting state (after K5b)

K5b (commit `45f9a94`) already merged KB source onto `apps/knowledge-service/` single
authority. The gateway still contained:

1. `src/main.py` — Confluence scheduler + sync-service initialisation (still live).
2. `src/services/knowledge/confluence/*.py` — identical copy of the code in kb-service
   (byte-for-byte per K5b report); K5c deletes these.
3. `src/services/knowledge/{embedding,vlm_service}.py` — two files kept because
   gateway's Confluence path imported them. Once Confluence code is gone they become
   dead and are deleted.
4. `src/api/v1/confluence.py` — 1 212 lines of REST API. Routes depend on
   `app.state.confluence_sync_service` — once the scheduler init block is removed,
   those routes will return 503 until the follow-up wires a proxy. This is
   acceptable because: (a) the UI flag `GATEWAY_CONFLUENCE__ENABLED` already gates
   visibility, and (b) the existing `proxy_to_kb_service` helper in
   `_proxy_utils.py` is the template for a thin replacement.

## Per-file migration decision table

| File / Module | Status | Decision | Rationale |
|---|---|---|---|
| `src/main.py::ConfluenceScheduler` init (lines 474-542) | still-in-gateway | **delete** | This is the ingestion worker we're removing. kb-service lifespan takes over. |
| `src/main.py::shutdown::confluence_scheduler.stop()` (lines 595-598) | still-in-gateway | **delete** | Mirror of the init; no scheduler → no stop. |
| `src/main.py::shutdown::kb_worker.stop()` / `kb_service.close()` (lines 600-606) | still-in-gateway | **delete** | K5b removed the init; shutdown block still calls `.stop()` on attributes that are never set. Dead code. |
| `src/services/knowledge/confluence/*.py` (7 files, 8 147 LOC) | dup-in-kb-service | **keep as dead** | kb-service has byte-identical copies. These stay imported by `src/api/v1/confluence.py` (the API module imports `ConfluenceAccessDeniedError`/`ConfluenceSyncError`/`ConfluenceClient`/`ConfluenceCredentials` at module-/function-scope). Deleting them would break the router's module-level import, which would break gateway startup. Converting the router to a thin proxy is the proper fix but is ~1 200 lines of route-by-route conversion work — scoped to a follow-up (K5d/K5e, Polaris #5 "数据路径单一"). **Key invariant**: the code is imported but never invoked for ingestion because the scheduler init block in `main.py` is deleted, the sync-service instance is never constructed, and `get_confluence_sync_service` returns 503 for any caller. |
| `src/services/knowledge/embedding.py` | kept-as-dead | **keep** | Only importer is `confluence/sync_service.py`, itself now only used for class-export to the router. The actual `create_embedding` call path is no longer exercised at runtime. |
| `src/services/knowledge/vlm_service.py` | **actively used** | **keep** | Gateway imports `DashScopeVLMService` in `src/main.py` (line 1057) for the assistant-service's multimodal VLM fallback — NOT ingestion. Keep. |
| `src/api/v1/confluence.py` | still-in-gateway | **keep; return-503** | Router is registered in `src/api/router.py`; deleting breaks the module-level import. Keep the module but with the sync-service unset the `get_confluence_sync_service` dependency returns 503. Full proxy conversion is follow-up work (see "Deferred" below). |
| `src/api/schemas/confluence.py` | still-in-gateway | **keep** | Pure Pydantic schemas, no runtime cost. Used by `confluence.py` router. |
| `src/services/knowledge/kb_proxy_client.py` | still-in-gateway | **keep** | This is the thin HTTP client to kb-service — what we WANT in the gateway. |
| `apps/knowledge-service/src/knowledge_service/main.py::lifespan` | missing confluence wiring | **add** | Initialise `ConfluenceSyncService` + `ConfluenceScheduler` in kb-service lifespan, gated on the same env flag gateway previously used. |
| `apps/knowledge-service/src/knowledge_service/auth/gateway_secret_mw.py` | missing | **add** | New file. Reuses `ai_gateway_core.auth.gateway_secret.GatewaySecret` (no fork). |
| `apps/knowledge-service/src/knowledge_service/main.py` middleware chain | missing HMAC | **add** | Install middleware like assistant-service does (env-gated, `/health` exempt, `allow_anonymous` fallback). |
| `docker-compose.yml` `knowledge-service.environment` | missing secret | **add one line** | `GATEWAY_ASSISTANT_SHARED_SECRET: "${GATEWAY_ASSISTANT_SHARED_SECRET}"` — same secret as assistant-service hop; single shared secret keeps deployment simple. |

## Why single secret (not a new `GATEWAY_KNOWLEDGE_SHARED_SECRET`)

`_proxy_utils.py::_build_signer` already prefers `GATEWAY_KNOWLEDGE_SHARED_SECRET`
and falls back to `GATEWAY_ASSISTANT_SHARED_SECRET`. We don't set the former
anywhere — the gateway signs its kb-service calls with the same secret it uses
for assistant-service. Threat model is identical (sibling container on the
Docker bridge) and a single secret means operators rotate one value, not two.
If a future audit demands domain-isolated secrets, the fallback path in
`_proxy_utils.py::_build_signer` is already there — just set the new env var
and drop the fallback.

## Deferred (documented in acceptance-K5c.md)

1. **`src/api/v1/confluence.py` → thin proxy to kb-service.** The 1 212-line
   file contains many pure-DB endpoints plus a few ingestion-triggering ones. Full
   conversion is K5d/K5e scope (item #5 "数据路径单一"). For K5c, the functional
   outcome (gateway does not ingest) is achieved by removing the scheduler init;
   the router still exists but `get_confluence_sync_service` raises 503 for any
   caller. kb-service does not yet expose `/api/v1/confluence/*` routes — that's
   part of the future conversion.
2. **Confluence REST API surface on kb-service.** Once (1) is done the REST
   endpoints move to kb-service and gateway proxies to them. Today only the
   scheduler/worker runs in kb-service.
