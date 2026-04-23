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

- [x] Migrate `/tools` (GET) — done in round 2 below
- [x] Migrate `/policies` (GET) — done in round 2 below
- [x] Migrate `/chat` (POST, non-stream) — done in round 2 below
- [ ] ~~Migrate `/runs/{id}` (GET)~~ — **deferred to 5c** (see round-2 rationale)
- [ ] ~~Migrate `/approvals/{id}` (POST)~~ — **deferred to 5c** (see round-2 rationale)
- [ ] Run runbook step 4 and paste stdout to close Polaris item 6 (still operator task)

Once the items above are ✓ and Polaris item 6 has prod evidence, update
this doc's "Polaris verdict" section and run a second pass of the e2e auth
test against prod (with a prod-synthesized admin JWT) to close item 7 end-to-end.

---

## Round 2 (same commit series, follow-up PR): /chat + /tools + /policies

### What shipped

| Route | GW side | AS side | Equivalence test |
|---|---|---|---|
| `POST /assistant/chat` | 7-line proxy branch (authz above stays) under `ASSISTANT_ROUTE_CHAT_PROXIED` | Already had full `AssistantService.chat(...)` call in `apps/assistant-service/.../api/routes/chat.py` — re-used | `test_chat_response_has_required_keys` + `test_as_chat_returns_gateway_schema_keys` |
| `GET /assistant/tools` | 4-line proxy branch under `ASSISTANT_ROUTE_TOOLS_PROXIED` | Upgraded stub to full gateway shape: added `when_to_use`, `when_not_to_use`; passes `user=user` to `list_tools()` for permission filter parity | `test_tools_response_has_required_keys` + `test_as_tools_returns_gateway_schema_keys` |
| `GET /assistant/policies` | 5-line proxy branch under `ASSISTANT_ROUTE_POLICIES_PROXIED` | New AS route calling `assistant.get_gateway_policies()` — same data source as GW side | `test_policies_response_has_required_keys` + `test_as_policies_returns_top_level_policies_key` |

Gateway new-code line counts (proxy branches):

```
/chat      — 7 lines (authz already above; + body-bytes read + proxy.forward)
/tools     — 4 lines
/policies  — 5 lines
```

All under the 15-line budget from the Roadmap.

### Why `/runs/{id}` and `/approvals/{id}` are TODO 5c, not 5b

Both routes read from `AssistantService.execution_gateway` — an in-memory
run/approval registry **scoped to the AssistantService instance that served
the originating chat call**. In the Phase 5b transitional state there are two
separate `AssistantService` instances:

1. Gateway's in-process one (`app.state.assistant_service` in `src/main.py`)
2. AS container's one (`apps/assistant-service/.../main.py` lifespan)

A run started on the GW side is invisible on the AS side and vice versa. A
per-route feature flag on `/runs/{id}` or `/approvals/{id}` would therefore
**leak 404s as soon as `/chat` starts proxying** — the run_id would be
registered in the AS AssistantService's execution_gateway while the client
queries the GW's, which has no knowledge of it.

The only honest migrations are:

- Externalise run / approval state (DB or shared redis) before flipping either
  route — **Phase 5c territory** (north star #5 "数据路径单一" covers this).
- OR migrate `/chat` + `/runs/{id}` + `/approvals/{id}` as a single atomic
  flip (all-or-nothing) so the state stays on one side.

Explicit `TODO(5c)` docstring comments landed on both gateway handlers
referencing this acceptance doc.

### Polaris north star (post-round-2)

| 北极星 | pre-round-2 | post-round-2 | 本轮变动 |
|---|---|---|---|
| 1 编译时解耦 (AS) | ✗ | ✗ | — |
| 2 源码单一权威 | ✓ / ? | ✓ / ? | — |
| 3 启动独立 | ✗ | ✗ | — |
| 4 运行时不共栈 | ✗ | ✗ | — (5c territory) |
| 5 数据路径单一 | ✗ | ✗ | — (run/approval state deferred, exactly this item) |
| 6 网络边界 (AS) | ✓ (pending runbook step 4) | ✓ (pending runbook step 4) | — |
| 6 网络边界 (KB) | ✗ | ✗ | — |
| 7 Auth 契约 (AS) | ✓ (admin + smuggle + 7 headers) | ✓ (unchanged, and hard contract tests still green) | **non-regression** across 3 more routes |

**Verdict:** Round 2 does **not** flip a new north-star item green — that's
intentional. It closes out the remaining Phase 5b "AS-as-real-target" work so
Phase 5c can start from a clean base with every simple GET + basic POST already
proxyable (default OFF, zero behaviour change for prod).

### Tests

Total migrated-route tests now: 12 (up from 6).
Full Phase-5 new-test surface: **55 green**
(e2e auth 6 + route flags 7 + equivalence 12 + proxy 12 + gateway_secret 12 + gateway_secret middleware 6 = 55).

Run:
```
uv run pytest tests/contract tests/proxy/test_service_proxy.py -v --no-cov
```

### Disallowed narrative reminder

Still forbidden in any commit message / PR body until Phase 5c+ close
north-star items 1/3/4/5: "extracted", "microservice complete",
"true isolation". Allowed: "routes proxied with in-process fallback",
"auth contract locked", "shape-equivalence verified".

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
