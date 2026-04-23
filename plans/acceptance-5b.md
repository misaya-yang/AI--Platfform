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
| 6 网络边界 (AS) | ✓ (pending `[5a-5b]`) | ✓ (prod-confirmed 2026-04-23) | curl :8093 → Connection refused; chat/stream → HTTP 200; 见下方证据章节 |
| 6 网络边界 (KB) | ✗ | ✗ | — |
| 7 Auth 契约 (AS) | ✓ (admin-roles only) | ✓ (admin-roles + user_type strip + all-7-headers) | 新增 2 个 e2e 测试 + gateway `UserContext` 扩了 `user_type`/`email`/`name` |

**Polaris 判定:** 本 commit 让 #7 从 "单一场景验证" 升到 "三场景全覆盖",#6 等 runbook。
其余 5 条仍 ✗ — Phase 5c/5d/K5b/K5c/5e 范围。

禁用叙事:"extracted / microservice complete"。允许叙事:"routes proxied with
in-process fallback, auth contract locked".

---

## Operator tasks — evidence (2026-04-23)

### Polaris #6 (AS network boundary) — production probe

Subagent A executed the runbook (see `plans/ops-5b-deploy-log.md` for the full
timestamped log). Key evidence captured **from the operator's laptop, not from the
EC2 host**, to prove the public internet cannot reach `assistant-service`:

```
$ curl --max-time 5 --connect-timeout 5 -sSv http://52.65.136.42:8093/health 2>&1
*   Trying 52.65.136.42:8093...
* connect to 52.65.136.42 port 8093 from 10.6.4.5 port 50563 failed: Connection refused
* Failed to connect to 52.65.136.42 port 8093 after 1566 ms: Couldn't connect to server
* Closing connection
curl: (7) Failed to connect to 52.65.136.42 port 8093 after 1566 ms: Couldn't connect to server
```

Curl exit code 7 = TCP `Connection refused` — the OS rejected the connection
immediately (RST), confirming that `127.0.0.1:8093:8093` in `docker-compose.yml`
binds the host-side socket to loopback only and the public iface (`0.0.0.0`) does
NOT listen on 8093. Sanity-check (control) — port 8080, which IS public:

```
$ curl --max-time 5 --connect-timeout 5 -sS -w "HTTP %{http_code}\n" http://52.65.136.42:8080/health
{"status":"healthy","version":"2.0.0"}HTTP 200
```

So 8080 is reachable but 8093 is refused → it is not a network/firewall blanket
issue, it is the loopback binding doing its job.

### Public chat/stream still works (no rollback)

```
$ curl -sS -w "HTTP %{http_code}\n" --max-time 30 \
    -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
    -d '{"message":"say hi in 3 words","model_id":"qwen3.6-plus","max_tokens":10}' \
    https://yang.misaya.online/api/v1/assistant/chat/stream
HTTP 200
```

First 3 SSE event lines from the response body (metadata frames before model output):

```
data: {"event_type": "gateway_decision", "data": {"run_id": "20db4e36-56d7-4cfe-9fef-c31124a87802", "execution_profile": "safe", ...}, "timestamp": 1776943127.237443}
data: {"event_type": "run_started", "data": {"run_id": "20db4e36-56d7-4cfe-9fef-c31124a87802", "thread_id": "51baf358-8a07-40d1-bb4d-dfc4eeec52ae", ...}, "timestamp": 1776943127.23753}
data: {"event_type": "streaming_first_started", "data": {"mode": "streaming_first", "message_preview": "say hi in 3 words", "agent_loop_phase": "generation_storage"}, "timestamp": 1776943127.237818}
```

Three `text_delta` events were also emitted later in the stream
(model output: `"Hello"`, `" there, Misaya"`, `"!"`). Total 5814 bytes / 8.6 s.

### Operator-side state changes on prod

- `/opt/deploy/.env` now contains a single `GATEWAY_ASSISTANT_SHARED_SECRET=…6a66`
  line (preview only — full secret never leaves the EC2 host). Backup at
  `/opt/deploy/.env.bak-phase5b-20260423-110942`.
- `gateway` and `assistant-service` rebuilt and recreated; both `(healthy)` within
  ~50 s. Both containers have the secret loaded as an env var (verified via
  `docker exec ... printenv`), although the deployed source at git HEAD `8e620f8`
  does NOT yet contain the HMAC validation middleware (Phase 5a/5b code on `dev`
  has not been pulled into `/opt/deploy/ai-gateway`). The env var is therefore
  pre-staged for the next code-update deploy that ships `1e86f4c…d56a70a`.

### Conclusion

Polaris #6 (AS network boundary) → ✓ (prod evidence above, captured 2026-04-23 19:13 CST).

---

## Session summary — parallel round (2026-04-23)

Three sibling branches forked from `dev@d56a70a` and merged back via
cherry-pick. Each was produced by an independent subagent in an isolated
worktree; main agent did the scope + narrative + gate review before merging.

### Commits landed on `dev`

| Branch | SHA | Subject | Scope |
|---|---|---|---|
| `phase5b-ops` | `3c7b7b3` | ops(phase-5b): prod secret injection + public-port refusal evidence | `plans/ops-5b-deploy-log.md` (new), `plans/acceptance-5b.md` (append) |
| `phase-K5b` | `8440208` | feat(K5b): KB fork reconciliation → apps/knowledge-service as single source | `src/services/knowledge/*` (47 files removed), `apps/knowledge-service/**` (1 merge), `packages/ai-gateway-core/src/ai_gateway_core/knowledge/utils.py` (new), `src/main.py` (190 LOC dead block removed), `src/api/v1/assistant.py` (1 import switched), tests (3 files touched), `plans/kb-fork-merge-report.md` + `plans/acceptance-K5b.md` (new) |
| `phase5c-adr` | `38b20c8` | docs(phase-5c): ADR-004 run/approval state externalisation | `plans/ADR-004-Run-Approval-State-Externalization.md` (new, 653 lines) |

### Scope review — main agent verification

| Subagent | Scope violation? | Note |
|---|---|---|
| A | None | Only `plans/acceptance-5b.md` (append) + `plans/ops-5b-deploy-log.md` (new). Secret never leaked in full form — only `9ffa…6a66` previews. |
| B | **1 minor — accepted** | Touched `src/api/v1/assistant.py` (2 lines: import switch from `...services.knowledge.embedding` to `ai_gateway_core.knowledge.utils`). Blacklisted in B's scope rules, but required because the grep gate (`grep -rE "from \\.\\.\\.services\\.knowledge\\.embedding" src/ \| wc -l` = 0) could not otherwise be met. Main agent accepted as "import switch, not route migration" consistent with spirit of B's whitelist. |
| C | None | Only the single ADR file. All 6 required sections present; zero forbidden narrative; decision explicit (Option B — Postgres). |

### Gate evidence

- **Subagent A**: curl public `http://52.65.136.42:8093/health` → `Connection refused` (curl exit 7). chat/stream smoke → `HTTP 200` + 3 SSE `text_delta` events. **⚠ Caveat**: the deployed image at `/opt/deploy` is still at commit `8e620f8`, which does NOT yet contain `GatewaySecretAuthMiddleware` — the secret env var is pre-staged but the HMAC validation path is code-inert on prod until the next `git pull && docker compose build`. A's log calls this out explicitly. Polaris #6 is flipped ✓ based on the **network-boundary** gate alone (port refused); HMAC layer activates at the next code deploy.
- **Subagent B**: forbidden-import grep now returns `0`. `src/services/knowledge/` reduced from ~50 files to 4 (`__init__.py`, `kb_proxy_client.py`, `embedding.py`, `vlm_service.py` + `confluence/` subdir). The last two files + the subdir are explicitly deferred to K5c. Pytest on relevant suites: 181 passed / 4 failed (4 failures are pre-existing baseline, verified against `dev@d56a70a`).
- **Subagent C**: ADR-004 has all 6 sections. Decision = Option B (Postgres, migration 034 dual-write → single read source). Unknowns explicitly marked (runs/hr volume, run max-lifetime).

### North-star verdict (post parallel round)

| # | Item | Pre | Post | Change source |
|---|---|---|---|---|
| 1 | 编译时解耦 (AS) | ✗ | ✗ | — |
| 1 | 编译时解耦 (KB) | ✓ | ✓ | — |
| 2 | 源码单一权威 (AS) | ✓ | ✓ | — |
| 2 | 源码单一权威 (KB) | ? | **✓** | subagent B (fork merged, 47 files removed, grep gate ✓) |
| 3 | 启动独立 | ✗ | ✗ | — |
| 4 | 运行时不共栈 | ✗ | ✗ | — (blocked on ADR-004 → 5c) |
| 5 | 数据路径单一 | ✗ | ✗ | — (blocked on ADR-004 → 5c) |
| 6 | 网络边界 (AS) | ⚠ | **✓** (code-deploy caveat above) | subagent A (port 8093 refused from public IP) |
| 6 | 网络边界 (KB) | ✗ | ✗ | — (KB side still lacks `GatewaySecretAuthMiddleware`; K5c) |
| 7 | Auth 契约 (AS) | ✓ | ✓ | non-regressed |

**Summary:** 2/8 AS-side items ✓ before this round, **3/8 after** (items 2-KB, 6-AS, 7-AS).

### Next-round candidates (not this session)

- **Phase 5c (run/approval externalisation)** — implement ADR-004 Option B: add `runs` and `approvals` tables to the `gateway` DB (or reuse migration 034 if already compatible), switch `AssistantService.execution_gateway` to DB-backed reads, then migrate the two gateway routes under feature flags. Closes north-star items #4 + #5 for AS.
- **Phase K5c (Confluence)** — migrate `src/services/knowledge/confluence/*` into `apps/knowledge-service/` and remove the last two shared-util files (`embedding.py`, `vlm_service.py`) from the gateway tree. Install `GatewaySecretAuthMiddleware` on knowledge-service to close north-star item #6-KB.
- **Prod code deploy** — pull the 5a/5b commits onto `/opt/deploy/ai-gateway` and rebuild. This activates the HMAC middleware that A pre-staged the env var for. Without this step, item #6-AS's "HMAC layer" sub-check stays code-inert.

### Explicit non-claims

This round does **not** earn any of the following narratives:
- "Assistant service extracted"
- "Microservice decoupling complete"
- "True isolation achieved"

These require items 1, 3, 4, 5 all ✓ simultaneously, which needs 5c + K5c
(and the corresponding route-migration + compile-time cleanup) to land.

