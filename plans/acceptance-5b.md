# Phase 5b — Acceptance Report (partial, in-progress)

> Roadmap: `plans/Roadmap-Post-5a-Extraction-2026-04-23.md` Phase 5b.
> Polaris targets this Phase: **item 6** (network boundary) + **item 7** (auth contract).

## Status at this commit

Phase 5b's "before you start searching routes, do two things" preamble is done.
The **first three routes** (`/models`, `/datasets`, `/config`) are wired under
feature flag — default OFF so production behaviour is unchanged.

| Deliverable | Status |
|---|---|
| e2e auth test covering admin roles / user_type smuggle / 7-header transit | ✓ `tests/contract/test_auth_e2e.py` (6 tests, all green) |
| Per-route feature-flag helper + tests | ✓ `src/api/v1/_route_flags.py` + `tests/contract/test_route_flags.py` (7 tests) |
| Gateway `UserContext` extended with `user_type` / `email` / `name` | ✓ `packages/ai-gateway-core/src/ai_gateway_core/auth/_core.py` |
| Gateway `UserResolver` populates the 3 new fields from JWT claims | ✓ `src/core/auth/user_resolver.py` |
| AS-side `/models` / `/config` / `/datasets` upgraded to match gateway's pydantic shape | ✓ `apps/assistant-service/src/assistant_service/api/routes/models.py` |
| Gateway `/models` / `/datasets` / `/config` proxy under flag | ✓ `src/api/v1/assistant.py` |
| Shape-equivalence contract tests for the three routes | ✓ `tests/contract/test_migrated_routes_equivalence.py` (6 tests) |
| Remaining routes (`/tools`, `/policies`, `/runs`, `/approvals`, `/chat`) | ✗ TODO — follow the same pattern |
| Prod secret injection + rebuild | ✗ operator task (runbook below) |
| Live `curl http://<public>:8093` refused | ✗ depends on runbook completion |

**Polaris verdict at this commit:** item 7 (Auth 契约) is **fully verified in-process**
by the 6 e2e tests. Item 6 (网络边界) is **half-done** — code + compose are correct
(Phase 5a), but the prod secret injection + `curl` refusal test are operator tasks
tracked below.

## Phase 5b prompt compliance

The Roadmap Phase 5b prompt has 3 hard rules:

1. **e2e auth test BEFORE migrating any route** — done (see above).
2. **Gateway new-route implementation ≤ 15 lines** — verified:
   ```
   /models  proxy branch = 4 lines
   /datasets proxy branch = 4 lines
   /config   proxy branch = 4 lines
   ```
3. **AS-side route must exist, not be a stub** — fixed:
   `apps/assistant-service/src/assistant_service/api/routes/models.py` now
   returns the full pydantic schema the gateway's clients consume. The
   equivalence test checks every required key is present.

## Prod secret-injection runbook

**For the operator to run on 52.65.136.42.** Each step's stdout should be
captured into a follow-up PR comment.

### 0. Back up `.env`
```bash
ssh <prod> 'cp /opt/deploy/.env /opt/deploy/.env.bak-phase5a-$(date +%Y%m%d-%H%M%S)'
```

### 1. Generate the shared secret (64 hex chars = 256 bits of entropy)
```bash
ssh <prod> 'echo "GATEWAY_ASSISTANT_SHARED_SECRET=$(openssl rand -hex 32)" >> /opt/deploy/.env'
ssh <prod> 'grep GATEWAY_ASSISTANT_SHARED_SECRET /opt/deploy/.env'
```
(The `grep` line is for visual confirmation that the env var landed — the
secret value itself is fine to see in shell history on the prod host.)

### 2. Rebuild gateway + assistant-service images with the Phase 5a code
`docker-compose.yml` already carries the `:?required` guard on
`GATEWAY_ASSISTANT_SHARED_SECRET` — if step 1 was skipped, step 2 fails
fast with `GATEWAY_ASSISTANT_SHARED_SECRET is required for gateway<->assistant-service auth`.
```bash
ssh <prod> 'cd /opt/deploy && git fetch origin && git checkout dev && git pull'
ssh <prod> 'cd /opt/deploy && docker compose build gateway assistant-service'
ssh <prod> 'cd /opt/deploy && docker compose up -d --force-recreate gateway assistant-service'
ssh <prod> 'cd /opt/deploy && docker compose ps gateway assistant-service'
```

### 3. Verify gateway can talk to assistant-service (HMAC roundtrip)
```bash
ssh <prod> 'JWT=$(curl -sSf -X POST http://127.0.0.1:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"123456.dc\",\"email\":\"admin@hejazfs.com.au\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[\"access_token\"])"); \
  curl -sS -w "\nHTTP %{http_code}\n" --max-time 30 \
    -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
    -d "{\"message\":\"say hi in 3 words\",\"model_id\":\"qwen3.6-plus\",\"max_tokens\":10}" \
    http://127.0.0.1:8080/api/v1/assistant/chat/stream | tail -10'
```
Expected: HTTP 200 + SSE events. If HTTP 401 → secret mismatch. If HTTP 502 →
assistant-service rejected the HMAC header.

### 4. Verify public 8093 is refused (Polaris item 6 evidence)
```bash
# From YOUR laptop, not the prod host — testing the PUBLIC-ip attack surface.
timeout 5 curl -sv http://52.65.136.42:8093/health
# Expected: Connection refused / timeout. Anything that returns HTTP is a FAIL.
```
Paste this stdout into the PR under a **Phase 5b runbook § step 4** heading.

### 5. (Optional) Flip `/models` to proxy mode for a live canary
On prod, to flip ONE route to the new path while keeping all others on the
in-process handler:
```bash
ssh <prod> 'echo "ASSISTANT_ROUTE_MODELS_PROXIED=true" >> /opt/deploy/.env'
ssh <prod> 'cd /opt/deploy && docker compose up -d --force-recreate gateway'
# Observe 10-15 min. If anything degrades, flip back with `false` + recreate.
```

## Remaining Phase 5b work (not in this commit)

- [ ] Migrate `/tools` (GET list) — depends on AS-side implementation of
      `list_tools()` matching gateway's `ToolInfoResponse` shape
- [ ] Migrate `/policies` (GET) — depends on `tenant_tool_policy` service
      being reachable from AS
- [ ] Migrate `/runs/{id}` (GET) — needs run registry on AS side
- [ ] Migrate `/approvals/{id}` (POST) — needs approval manager on AS side
- [ ] Migrate `/chat` (non-stream POST) — larger; touches model_registry +
      session_manager; realistically a separate PR
- [ ] Run runbook step 4 and paste stdout to close Polaris item 6

Once the 5 items above are ✓ and Polaris item 6 has prod evidence, update
this doc's "Polaris verdict" section and run a second pass of the e2e auth
test against prod (with a prod-synthesized admin JWT) to close item 7 end-to-end.

## North star (post this commit, same structure as acceptance-5a.md)

| 北极星 | pre-5b | this commit | 本 commit 变动 |
|---|---|---|---|
| 1 编译时解耦 (AS) | ✗ | ✗ | — |
| 2 源码单一权威 | ✓ / ? | ✓ / ? | — |
| 3 启动独立 | ✗ | ✗ | — |
| 4 运行时不共栈 | ✗ | ✗ | — |
| 5 数据路径单一 | ✗ | ✗ | — |
| 6 网络边界 (AS) | ✓ (pending `[5a-5b]`) | ✓ (pending runbook step 4) | 无本质推进,等 prod 验证 |
| 6 网络边界 (KB) | ✗ | ✗ | — |
| 7 Auth 契约 (AS) | ✓ (admin-roles only) | ✓ (admin-roles + user_type strip + all-7-headers) | 新增 2 个 e2e 测试 + gateway `UserContext` 扩了 `user_type`/`email`/`name` |

**Polaris 判定:** 本 commit 让 #7 从 "单一场景验证" 升到 "三场景全覆盖",#6 等 runbook。
其余 5 条仍 ✗ — Phase 5c/5d/K5b/K5c/5e 范围。

禁用叙事:"extracted / microservice complete"。允许叙事:"routes proxied with
in-process fallback, auth contract locked".
