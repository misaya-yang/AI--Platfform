# Phase K5c — Acceptance Report

> Roadmap: `plans/Roadmap-Post-5a-Extraction-2026-04-23.md` Phase K5c.
> Polaris target this Phase: **item #6-KB (网络边界, KB)** — install the HMAC
> `X-Gateway-Secret` middleware on `knowledge-service` so sibling containers
> on the Docker bridge network can no longer impersonate users.

Companion doc: `plans/k5c-migration-plan.md` (per-file decision table).

---

## Verdict

| Polaris item | pre-K5c | this commit | 本轮变动 |
|---|---|---|---|
| 1 编译时解耦 (AS) | ✗ | ✗ | — |
| 2 源码单一权威 (KB) | ✓ | ✓ | — (K5b closed it) |
| 3 启动独立 | ✗ | ✗ | — |
| 4 运行时不共栈 | ✗ | **partial** | gateway no longer runs KB ingestion / Confluence polling; reads only through `KBProxyClient`. Not fully ✓ until `src/api/v1/confluence.py` is proxy-converted (K5d). |
| 5 数据路径单一 | ✗ | ✗ | — (K5d/K5e territory) |
| 6 网络边界 (AS) | ✓ | ✓ | — |
| **6 网络边界 (KB)** | **✗** | **✓** | **本轮关闭** — `GatewaySecretAuthMiddleware` installed on knowledge-service, env-gated on `GATEWAY_ASSISTANT_SHARED_SECRET`. Rejects unsigned traffic with `401 AUTH_DENIED`; `/health` exempt. |
| 7 Auth 契约 (AS) | ✓ | ✓ | — (HMAC code reused verbatim from `ai_gateway_core`, no fork) |

**Verdict: this commit flips Polaris #6-KB from ✗ to ✓**, and does not regress any
item previously ✓. Item #4 moves from ✗ to partial (the ingestion / scheduler
runtime is gone; the REST surface proxy is follow-up).

---

## File inventory

### Added

- `apps/knowledge-service/src/knowledge_service/auth/gateway_secret_mw.py` — thin
  Starlette middleware wrapping `ai_gateway_core.auth.gateway_secret.GatewaySecret`.
  No crypto fork. Mirrors the assistant-service impl 1-to-1 except imports and
  module docstring.

### Modified

- `apps/knowledge-service/src/knowledge_service/auth/__init__.py` — re-exports
  the middleware + existing `UserContext` / `get_user_context`.
- `apps/knowledge-service/src/knowledge_service/main.py` — env-gated middleware
  registration after CORS. Follows assistant-service's activation rule:
  secret-set → active; secret-unset + `allow_anonymous=false` → warn; secret-unset +
  `allow_anonymous=true` → pass-through for dev. `/health` + `/metrics` exempt.
- `docker-compose.yml` — **+3 lines** added to the `knowledge-service.environment`
  block: a comment + one env var (`GATEWAY_ASSISTANT_SHARED_SECRET`) with the
  same `:?...` required-value marker used by assistant-service. Port/publish
  posture unchanged (`127.0.0.1:8092`).
- `src/main.py` — **-82 lines / +13 lines** (net -69). Deleted the entire
  Confluence scheduler + sync-service init block (`if settings.confluence.enabled`
  — 69 lines) and replaced with a one-line log statement. Deleted the matching
  shutdown handlers for `confluence_scheduler`, `knowledge_worker`, and
  `knowledge_service` (the last two were already dead after K5b; removed for
  clarity).

### Deleted — none this commit

See `plans/k5c-migration-plan.md` "Per-file migration decision table" for
rationale. Short version: `src/services/knowledge/confluence/*.py` and
`src/services/knowledge/embedding.py` stay in-tree as dead imports because
`src/api/v1/confluence.py` (1 212 lines of REST routes) still imports from them
at module scope. Deleting breaks gateway module load. Converting the router to a
thin proxy is scoped to follow-up K5d/K5e (Polaris #5).

---

## Acceptance gates

### Gate K5c-1 — no in-process KB ingestion on gateway

```
$ grep -rnE "KnowledgeWorker\(|KnowledgeService\(|ConfluenceScheduler\(" src/
src//services/knowledge/confluence/scheduler.py:901:        cls._instance = ConfluenceScheduler(sync_service, **kwargs)
```

**PASS.** Main.py contains zero constructor calls. The single remaining match
is inside `SchedulerManager.start()` — a factory classmethod that is not wired
into anything in gateway runtime; it's only reachable when someone calls
`SchedulerManager.start(...)` from outside, and no caller in `src/` does. The
module lives only as an import target for `src/api/v1/confluence.py` exception
classes; see migration plan for deletion rationale.

Main.py-only check (stricter):

```
$ grep -nE "KnowledgeWorker|KnowledgeService|ConfluenceScheduler|VisionPDFProcessor|HierarchicalIndexer|VLMOCRService|SummaryGenerator" src/main.py
465:        # The pre-K5b in-process initialisation (KnowledgeService, KnowledgeWorker,
466:        # VisionPDFProcessor, HierarchicalIndexer, VLMOCRService …) was gated
```

Both matches are comments inside the "KB runs in a microservice" logger block.

### Gate K5c-2 — gateway doesn't log KB processing when knowledge-service is down

**BLOCKED** — this worktree does not have a live `docker-compose` stack. The
current `docker compose ps` fails on missing `POSTGRES_PASSWORD` env interpolation
because the worktree has no `.env` file (production values live in
`/opt/deploy/.env` on the server per `reference_server_deployment.md`).

Repro recipe for the operator (runs on prod or any host with a populated `.env`):

```
# From the repo root, after phase-K5c is deployed:
docker compose stop knowledge-service
sleep 60
docker compose logs gateway --since 60s 2>&1 | grep -iE "embedding|qdrant upsert|processing document|processing confluence"
# Expected: zero lines.
docker compose start knowledge-service
```

**Static evidence** that the gate will pass:

1. `src/main.py` no longer imports `ConfluenceScheduler`, `ConfluenceSyncService`,
   `KnowledgeService`, or `KnowledgeWorker` at runtime (verified by Gate K5c-1).
2. No task is scheduled onto `app.state.knowledge_worker` or
   `app.state.confluence_scheduler` because neither attribute is ever set.
3. The only KB access from gateway runtime is `KBProxyClient` (`src/services/
   knowledge/kb_proxy_client.py`) — an HTTP client to knowledge-service. When
   knowledge-service is down it raises `httpx.ConnectError`, logged by the proxy
   as "KB Service unreachable" and returned as 503 to the caller. No "embedding"
   / "qdrant upsert" / "processing document" log lines are emitted.

### Gate K5c-3 — knowledge-service rejects unsigned request

**PASS (functional test, in-process).** Docker-network reproduction is
BLOCKED for the same reason as K5c-2.

Ran against the built FastAPI app with an in-process `TestClient`:

```
$ GATEWAY_ASSISTANT_SHARED_SECRET="0123456789abcdef0123456789abcdef" \
    KNOWLEDGE_APP__ALLOW_ANONYMOUS=false \
    .venv/bin/python -c "
from fastapi.testclient import TestClient
from knowledge_service.main import create_app
from knowledge_service.config import Settings
app = create_app(Settings())
c = TestClient(app, raise_server_exceptions=False)

r = c.get('/api/v1/knowledge/datasets')
print('unsigned status:', r.status_code)
print('unsigned body:', r.text[:120])

r = c.get('/health')
print('health status:', r.status_code, r.text[:60])
"
[...]
unsigned status: 401
unsigned body: {"code":"AUTH_DENIED","message":"Missing or invalid X-Gateway-Secret"}
health status: 200 {"status":"ok","service":"knowledge-service"}
```

Signed request (same test, using the `GatewaySecret.sign()` helper) returned
`500` — which means the middleware let the request through and the route
handler tried to touch a nonexistent DB. The important bit is "not 401" — the
HMAC gate let a valid signer through.

```
$ [...] signer = GatewaySecret(secret='0123...ef')
$ [...] hdr = signer.sign()
$ r = c.get('/api/v1/knowledge/datasets', headers={'X-Gateway-Secret': hdr, 'X-User-Id':'u1', 'X-Tenant-Id':'t1'})
signed status: 500 (not 401 — middleware passed)
```

Docker-network reproduction (for the operator):

```
docker compose exec gateway curl -sSo /dev/null -w '%{http_code}\n' http://knowledge-service:8092/api/v1/knowledge/datasets
# Expected: 401
```

### Gate K5c-4 — knowledge-service ingestion tests run green

Per the plan's fallback: `apps/knowledge-service/tests/` does not exist (all KB
tests live at repo root under `tests/unit/` and `tests/knowledge/`). The full
relevant unit suite:

```
$ .venv/bin/python -m pytest tests/unit/test_section_traceability.py tests/unit/test_acl_permissions.py tests/unit/test_islamic_metadata.py --no-cov
[...]
============================== 67 passed in 2.19s ==============================
```

**PASS — 67/67.** These are the KB-adjacent unit tests (chunking/section
traceability, proxy ACL checks, Islamic metadata extraction). The broader
`tests/knowledge/` directory is full of live-integration scripts (`requests.post`
to a running server) — out of scope for a worktree smoke run and pre-dated K5c.

Additional smoke: kb-service boots end-to-end with HMAC middleware active.

```
$ GATEWAY_ASSISTANT_SHARED_SECRET="0123456789abcdef0123456789abcdef" \
    .venv/bin/python -c "from knowledge_service.main import create_app; from knowledge_service.config import Settings; a = create_app(Settings()); print([m.cls.__name__ for m in a.user_middleware])"
[...]
gateway_secret_middleware_active allow_anonymous=False
[...]
['BaseHTTPMiddleware', 'GatewaySecretAuthMiddleware', 'CORSMiddleware']
```

Gateway also imports cleanly with K5c applied:

```
$ .venv/bin/python -c "from src import main; print('gateway main imports ok')"
[...]
gateway main imports ok
```

### Gate K5c-5 — Polaris verdict table

See the table at the top of this document.

---

## Commit message (DRAFT — applied at commit time)

```
feat(K5c): HMAC middleware installed on knowledge-service; Confluence
ingestion moved out of gateway process

- knowledge-service: GatewaySecretAuthMiddleware (reuses ai_gateway_core
  HMAC verbatim, no fork); /health + /metrics exempt; env-gated on
  GATEWAY_ASSISTANT_SHARED_SECRET.
- gateway (src/main.py): deleted Confluence scheduler + sync-service
  init block; deleted dead shutdown handlers for KnowledgeWorker /
  KnowledgeService that were already unreferenced after K5b.
- docker-compose.yml: +3 lines on knowledge-service.environment —
  GATEWAY_ASSISTANT_SHARED_SECRET required-value with the same :?...
  marker as assistant-service (single shared secret across both hops).
- Closes Polaris #6-KB. #4 moves ✗ → partial (ingestion gone;
  confluence REST proxy conversion is K5d follow-up).
```

---

## Deferred to K5d/K5e

1. **`src/api/v1/confluence.py` thin-client conversion.** 1 212 lines of
   REST routes must be converted from direct `ConfluenceSyncService` calls
   to `proxy_to_kb_service(...)`. Once done, delete
   `src/services/knowledge/confluence/*.py` and `src/services/knowledge/embedding.py`.
2. **kb-service confluence lifespan wiring.** knowledge-service already owns
   the confluence Python modules but its lifespan does not start the
   `ConfluenceScheduler`. Wiring it requires porting the gateway-side
   `ConfluenceSettings` pydantic block into knowledge-service `config/`.
   When (1) ships, this block MUST ship with it — otherwise there is no
   scheduler polling pages anywhere. Today Confluence sync is effectively
   paused until (2) lands on kb-service. Confluence is a low-traffic
   feature, currently OFF by default (`GATEWAY_CONFLUENCE__ENABLED=false`);
   deployments with it enabled must re-enable via the proxy once (1) is
   shipped.
3. **Rotate key material.** Prod `GATEWAY_ASSISTANT_SHARED_SECRET` should be
   rotated after the knowledge-service deployment so a stale value cached by
   a compromised sibling container is invalidated. Single-secret model means
   the rotation affects both gateway→AS and gateway→KB hops; operators run
   the rotation by updating `/opt/deploy/.env` and restarting all three
   services in parallel. See `reference_server_deployment.md`.
