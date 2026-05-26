# Gateway Core Hardening Rollout Acceptance - 2026-05-26

> For agentic workers: this is the final P6 acceptance record for the Gateway core hardening rollout. Treat it as evidence, not as a replacement for rerunning the commands before later production changes.

**Target:** `https://yang.misaya.online`

**Deployment source:** GitLab `dev`

**Server:** `/opt/deploy/ai-gateway` on `ubuntu@52.65.136.42`

**Verdict:** PASS. P1, P2, P2A, P3, P4, P5, P6 local gates passed; production Gateway was deployed from GitLab `dev`; production AOP smoke passed for auth, RBAC, rate limiting, quota hard block, capacity headers, deterministic UAT admission, header spoof protection, audit/log visibility, and browser Playground traffic.

---

## Commit Range

Pre-rollout baseline:

- `ba4317b fix(model-control): treat provider unavailable as retryable`

Gateway and AOP rollout commits:

- `82ced92 fix(gateway): harden auth capability boundaries`
- `2b8f33a fix(gateway): enforce rate and quota policies`
- `8afe0d7 fix(gateway): return admitted rate limit headers`
- `d3c7136 fix(gateway): make usage accounting idempotent`
- `b4e8075 fix(gateway): harden observability audit traces`
- `14182f1 fix(gateway): harden billing and quota production paths`
- `cd5a031 fix(gateway): enforce langgraph run contracts`
- `00882f6 fix(gateway): add uat capacity admission controls`
- `0c2493b chore(gateway): format auth contract imports`
- `32e0cb7 fix(gateway): persist service capacity config safely`
- `201c52a fix(gateway): refresh proxy config after service updates`

Adjacent runtime/UI commits included in the deployed branch state:

- `4ef6033 fix(playground): hide internal model runtime errors`
- `b7f6e48 fix(playground): localize model failover notices`
- `2f456b6 fix(playground): keep model notices in english`
- `a3abc59 fix(model-failover): default agent fallback policy`
- `d76c177 fix(playground): hide successful model fallback notice`

---

## Local Gate Matrix

All commands used `~/miniconda3/bin/conda run -n ai_gateway` for Python gates.

| Gate | Command scope | Result |
|---|---|---|
| P1 auth/RBAC/capability/tenant/spoofing | `tests/core/auth/*`, `tests/api/test_permission_capabilities.py`, `tests/api/test_gateway_auth_context_contract.py`, `tests/api/test_gateway_capability_matrix.py`, `tests/api/test_gateway_tenant_isolation.py`, `tests/proxy/test_gateway_header_spoofing.py` | `74 passed` |
| P2 rate/quota/priority/billing | rate limit, runtime config, priority queue, quota enforcement, billing tests | `45 passed` |
| P2A inventory | `scripts/gateway_capacity_inventory.py --format json` | `missing_count: 0`, `mode: single-node`, `cluster_epoch: uat-2026-05`, all UAT services mapped to real budgets |
| P2A capacity | capacity resolver, admission control, shared upstream budget, capacity status API, UAT capacity contract | `12 passed` |
| P3 usage/billing idempotency | usage idempotency, cost accounting, quota sync, parser, aggregates, event publish, billing failure/shutdown | `32 passed` |
| P4 observability/audit/redaction | metrics freshness, admin audit events, trace propagation, log redaction, metrics recorder, proxy propagation | `18 passed` |
| P5 LangGraph/proxy contracts | LangGraph contract, model override injection/config, adapter, proxy auth, transparent proxy | `125 passed` |
| Frontend lint | `npm --prefix web run lint` | PASS, existing warnings only |
| Frontend build | `npm --prefix web run build` | PASS, chunk-size warning only |
| Final affected regression after hotfixes | combined P1/P2A/P4 affected suite | `104 passed` |
| Static checks | `ruff check`, `python -m compileall`, `git diff --check` | PASS |

Hotfix red/green evidence:

- `test_service_capacity_config_update_uses_registry_persistence_without_legacy_db_method` failed before `32e0cb7` because production storage does not expose `update_service_config`.
- The same test passed after persisting through `registry.storage.save(service)` and guarding the legacy hook.
- The cache invalidation assertion failed before `201c52a` because transparent proxy config stayed stale after service updates.
- The assertion passed after calling `proxy_config_loader.invalidate(service_id)` from the service config update path.

---

## Production Deployment

Source pushed:

```bash
git push gitlab HEAD:dev
```

Server update:

```bash
cd /opt/deploy/ai-gateway
git fetch origin dev
git checkout dev
git pull --ff-only origin dev
```

Service action:

- Rebuilt and recreated only `gateway` with the existing `/opt/deploy` Compose workflow.
- Did not run `docker compose down`.
- Did not overwrite `/opt/deploy/docker-compose.yml`.
- Did not apply database migrations.
- Preserved existing frontend port and compose customizations.

Post-deploy health:

- `ai-gateway-backend`: healthy
- `ai-gateway-frontend`: healthy
- `ai-gateway-redis`: healthy
- `imam-agent`: healthy
- `GET http://127.0.0.1:8080/health`: `200`
- Frontend route: `200`

Rollback reference:

- Previous final-hotfix deployed commit: `32e0cb7`
- Pre-rollout baseline commit: `ba4317b`
- Rollback command pattern: checkout the target commit in `/opt/deploy/ai-gateway`, then rebuild/recreate only the affected service with the existing Compose command.
- Data rollback needed: no schema migrations were applied. Runtime config changes used API rollback/cleanup.

---

## Production AOP Evidence

### Auth And Authorization

| Probe | Result | Request id |
|---|---|---|
| Missing API key | `401 Missing API key` | `cce56dcc-8d49-4da3-ad96-eeaf3caa415c` |
| Invalid API key | `401 Invalid API key` | `7ec1a566-04ee-441d-834b-cfcc03bfac29` |
| Malformed JWT | `401 Malformed token` | `2ddb18ae-e7c6-4bd2-8dce-487393e2b11f` |
| Expired JWT | `401 Token has expired` | `43db6f8e-e588-48ee-8427-ced5b3f1ea3a` |
| JWT not present in Redis token store | `401 Token has been revoked` | `5212a7fb-4d97-4e87-88c2-4b9977e870c1` |
| Admin JWT | `200` | `2d598fe1-116f-4523-a60c-a75db45692e5` |
| Stored non-admin JWT on admin route | `403 Missing permission: admin` | `2eb2ae95-8bb1-4e8a-8388-cc40ead77f0b` |

### Rate Limit

Temporary rule: `operation:assistant_list`, `requests=1`, `window=60`.

| Probe | Result | Request id |
|---|---|---|
| First `assistants/search` | `200`, admitted capacity headers present | `ec53a2df-e9ed-410d-b14e-1244ed5ad0e0` |
| Second `assistants/search` | `429 RATE_LIMIT_EXCEEDED`, `Retry-After: 60`, remaining `0` | `2f8bff51-11e8-4020-abff-7be338f74dea` |

Cleanup: temporary rate-limit rule deleted and traffic restored.

### Quota Hard Block

Temporary admin block returned `200` on creation. The next `runs/stream` call was denied before upstream:

- Status: `403`
- Message: `User is blocked: p6 final quota hard block smoke`
- Request id: `f21b7d1e-c1f6-45a8-9f4a-1dc577fe0c76`

Cleanup: admin user unblocked.

### Capacity And Admission

Service config update path was proven against production:

- `PUT /api/v1/config/services/local-2024-agent/config` returned `200`.
- PostgreSQL `service_config.capacity` persisted the update.
- Config API readback matched the persisted value.
- Original service capacity config was restored.
- Transparent proxy config cache was invalidated after the update.

Shared capacity smoke on real Sheikh Wahda no-model-cost proxy:

- Four concurrent `assistants/search` calls returned `4 x 200`.
- Every admitted response included Gateway capacity headers.
- Request ids:
  - `0dc1bb1e-7685-4ba2-8472-71b24d9b5a45`
  - `454ad1d1-3897-4ebc-9c19-361777edee42`
  - `34d7a6ea-687a-43a5-9b8b-6e6af7bd03a4`
  - `4c90a38f-6452-4bc7-bf2f-e8888edba116`

Deterministic UAT `3 concurrent / 4 requests` probe used a temporary no-model-cost slow service:

- Temporary container: `gateway-capacity-slow`
- Temporary service id: `uat-capacity-slow-1779772974`
- Capacity: `upstream_group=imam_agent`, `concurrency_limit=3`, `queue_max=0`, `queue_timeout_ms=100`
- Result: `3 x 200`, `1 x 503 GATEWAY_CAPACITY_EXHAUSTED`
- `503` request id: `b51017cc-53fb-4f23-ad87-e47a35cbea7a`
- `503` headers: `Retry-After: 1`, `X-Gateway-Capacity-Key: upstream.imam_agent`
- Cleanup: temporary service deleted and temporary container removed. Final checks showed no temporary service and no running `gateway-capacity-slow` container.

### Header Spoofing

Probe sent client-authored identity headers:

- `X-User-Id: evil-user`
- `X-Tenant-Id: evil-tenant`
- `X-GW-User-Id: evil-gw`

Result:

- Response status: `200`
- Request id: `4ba4f9f2-145f-47da-b348-a8e97dd242f3`
- Gateway logs for the same request id showed `tenant_id=default user_id=admin`, proving Gateway-authored identity won over spoofed client headers.

### Logs And Secret Scan

Server logs showed capacity decisions, rate-limit/quota denials, request ids, and header-spoof proof. Error scan returned no unexpected Gateway errors.

Secret scan result:

- No raw provider key, bearer token, cookie, authorization header, or auth token was found.
- One warning contained the literal environment variable name `VERTEX_API_KEY` in a deprecation warning. It was not a raw secret value.

---

## Browser Regression Evidence

Browser route: in-app Browser plugin against `https://yang.misaya.online`.

Browser screenshots could not be captured because the plugin timed out on `Page.captureScreenshot` for the tab. The browser regression therefore used DOM snapshots, console logs, and real UI interaction evidence from the same in-app Browser session.

| Flow | Result |
|---|---|
| `/services` | Rendered Services and Sheikh Wahda. No framework overlay. No console errors. |
| `/settings` Capacity tab | Rendered `Gateway Capacity`, `uat-2026-05`, `upstream.imam_agent`, `provider.google_gemini`, `single-node`, and real budget status. No console errors. |
| `/services` Sheikh Wahda Configure | Priority tab rendered capacity controls: `Capacity Status`, `real`, `Upstream Group`, `Queue Timeout`, `Save Capacity`, and `not_configured`. |
| `/playground` | Rendered Playground with Sheikh Wahda selected and composer available. |
| Real Playground send | Sent `P6-final-browser-1779773572582` through the UI. The conversation log contained the user prompt, a new assistant response, TTFT, latency, and token stats. |
| `/dashboard` | Rendered Monitoring Dashboard, Operations panel, service health, trace table, token usage, and trace details after load. |
| `/users` | Rendered Users route. No framework overlay. No console errors. |
| `/knowledge` | Rendered Knowledge route. No framework overlay. No console errors. |

Relevant console health:

- No browser console errors were observed during the checked routes.
- One existing warning was observed: `[Playground] activeSessionId doesn't belong to current service, clearing`.

Playground interaction evidence:

- Marker: `P6-final-browser-1779773572582`
- Type method: Browser DOM CUA keypress fallback
- Assistant response included `Wa Alaikum Assalam.`
- Observed latency: `6.79s`
- Observed TTFT: `6785ms`
- Observed tokens: `3079 (2461/618)`

---

## Final Code Review

Review scope:

- Gateway auth/capability/tenant/spoofing enforcement.
- Rate-limit runtime policy and response headers.
- Quota hard-block pre-check path.
- Usage/cost idempotency.
- Observability, audit, log redaction, metrics freshness.
- LangGraph/proxy contract gates.
- Capacity resolver, admission control, shared Redis budgets, capacity status APIs, and service config persistence/cache invalidation.
- Browser-visible services/settings/playground/dashboard surfaces related to Gateway controls.

Review result:

- No blocking correctness issue found in the final diff.
- No unrelated destructive or broad refactor was introduced.
- The two production-discovered defects were fixed with focused tests before completion:
  - service capacity persistence without the legacy DB hook,
  - proxy config cache invalidation after service config update.

Remaining risks:

- Production is still single-node Gateway mode for UAT evidence. Shared Redis budget tests pass locally, but multi-replica production proof remains a later infrastructure gate.
- Browser screenshot evidence is missing because the in-app Browser screenshot path timed out. DOM, console, and interaction evidence passed.
- `/dashboard` currently shows historical provider/model attribution gaps for older requests. P4 surfaces the warning, but old records are not backfilled.
