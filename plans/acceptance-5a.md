# Phase 5a — Acceptance Report

> PR description body. Gates are from
> `plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md` §6 Phase 5a.
> Verify script: `plans/verify-phase-5a.sh`.

## Scope

Phase 5a establishes the foundation for true extraction without migrating any
route. It ships:

- `packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py` — shared
  `ServiceProxy` + `CircuitBreaker` + `InMemoryCounter` (+ `CounterStore`
  protocol for Redis). Both `_assistant_proxy.py` and `_proxy_utils.py`
  are now thin glue around this single module.
- `packages/ai-gateway-core/src/ai_gateway_core/auth/gateway_secret.py` —
  HMAC `X-Gateway-Secret` `sign()` / `verify()` with replay protection.
- `apps/assistant-service/src/assistant_service/auth/gateway_secret_mw.py` —
  `GatewaySecretAuthMiddleware` that rejects unsigned requests with 401.
- Canonical strip-and-inject header list: the 7 `X-User-*` headers
  assistant-service reads, plus `X-Gateway-Secret`.
- 91 lines removed from `src/api/v1/assistant.py` — the unreachable body
  after the `chat_stream` proxy call (git diff: `91 deletions(-)`).
- `docker-compose.yml`: assistant-service port 8093 moved from `ports:`
  to `expose:`, removing public-IP binding.
- Contract + proxy tests (34 new):
  `tests/contract/test_gateway_secret.py`,
  `tests/contract/test_gateway_secret_middleware.py`,
  `tests/contract/test_auth_roles_e2e.py` (H-2 end-to-end),
  `tests/proxy/test_service_proxy.py`.

## Verdict

**5 of 6 top-level gates fully green. G5a-5 PASS on the in-process
sub-check `[5a-5a]`; the live-docker sub-check `[5a-5b]` is SKIPPED
and tracked as a Known gap below.** Phase 5a is accepted on the fast
path only — merging to `main` requires `VERIFY_DOCKER=1 bash
plans/verify-phase-5a.sh` to clear the deferred sub-check.

## Audit findings closed

| Finding | How it is closed |
|---|---|
| H-1 dead code in chat_stream | 91 lines deleted (see `src/api/v1/assistant.py` diff). GATE G5a-2. |
| H-2 roles lost at proxy boundary | gateway injects `X-User-Roles`; assistant-service parses it. End-to-end JWT → UserContext test at `tests/contract/test_auth_roles_e2e.py`. |
| H-3 strip list incomplete | strip list expanded to 7 headers + `x-gateway-secret`. GATE G5a-3. |
| H-4 sibling container → user impersonation | `GatewaySecretAuthMiddleware` rejects unsigned requests. GATE G5a-5 (sub-check `[5a-5a]`). |
| M-1 KB proxy diverged from assistant proxy | both now delegate to the shared `ServiceProxy`. GATE G5a-1. |
| M-2 mid-stream failure un-counted | `_stream_wrapped` catches, calls `on_failure()`, re-raises. |
| M-3 SSE buffered when content-length present | `is_sse` check forces streaming path. |
| M-4 module-level breaker silently per-worker | `CounterStore` protocol; `InMemoryCounter` docstring flags the caveat. |
| M-5 4xx spuriously closes breaker | `on_response()` only treats 2xx as success. |
| L-1 unused `import time` | `_assistant_proxy.py` rewritten, import is gone. |

## Acceptance gate stdout (verbatim)

Command: `bash plans/verify-phase-5a.sh`

```
=== G5a-1: shared proxy module exists and is imported on both sides ===
[G5a-1] PASS

=== G5a-2: no dead code after chat_stream return ===
ok
[G5a-2] PASS

=== G5a-3: identity-header strip list complete ===
ok — strip list covers ['x-tenant-id', 'x-user-email', 'x-user-id', 'x-user-name', 'x-user-roles', 'x-user-tier', 'x-user-type']
[G5a-3] PASS

=== G5a-4: gateway_secret contract tests pass ===
============================= test session starts ==============================
platform darwin -- Python 3.13.11, pytest-9.0.3, pluggy-1.6.0 -- /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
configfile: pyproject.toml
plugins: cov-7.1.0, asyncio-1.3.0, respx-0.23.1, anyio-4.13.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 12 items

tests/contract/test_gateway_secret.py::test_roundtrip PASSED             [  8%]
tests/contract/test_gateway_secret.py::test_request_id_preserved PASSED  [ 16%]
tests/contract/test_gateway_secret.py::test_mismatched_secret_rejected PASSED [ 25%]
tests/contract/test_gateway_secret.py::test_tampered_signature_rejected PASSED [ 33%]
tests/contract/test_gateway_secret.py::test_stale_timestamp_rejected PASSED [ 41%]
tests/contract/test_gateway_secret.py::test_future_timestamp_rejected PASSED [ 50%]
tests/contract/test_gateway_secret.py::test_missing_header_rejected PASSED [ 58%]
tests/contract/test_gateway_secret.py::test_malformed_header_rejected PASSED [ 66%]
tests/contract/test_gateway_secret.py::test_replay_rejected PASSED       [ 75%]
tests/contract/test_gateway_secret.py::test_replay_store_isolation_across_ids PASSED [ 83%]
tests/contract/test_gateway_secret.py::test_secret_length_guard PASSED   [ 91%]
tests/contract/test_gateway_secret.py::test_forged_header_does_not_poison_replay_store PASSED [100%]

============================== 12 passed in 0.01s ==============================
[G5a-4] PASS

=== G5a-5: assistant-service rejects unsigned request with 401 ===
[5a-5a] in-process middleware contract tests:
============================= test session starts ==============================
platform darwin -- Python 3.13.11, pytest-9.0.3, pluggy-1.6.0 -- /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
configfile: pyproject.toml
plugins: cov-7.1.0, asyncio-1.3.0, respx-0.23.1, anyio-4.13.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 6 items

tests/contract/test_gateway_secret_middleware.py::test_health_always_allowed PASSED [ 16%]
tests/contract/test_gateway_secret_middleware.py::test_missing_header_rejected_when_anonymous_off PASSED [ 33%]
tests/contract/test_gateway_secret_middleware.py::test_missing_header_allowed_when_anonymous_on PASSED [ 50%]
tests/contract/test_gateway_secret_middleware.py::test_invalid_signature_rejected PASSED [ 66%]
tests/contract/test_gateway_secret_middleware.py::test_valid_signature_accepted PASSED [ 83%]
tests/contract/test_gateway_secret_middleware.py::test_malformed_header_rejected PASSED [100%]

============================== 6 passed in 0.04s ===============================
[5a-5b] live docker check SKIPPED — set VERIFY_DOCKER=1 to include it
[G5a-5] PASS

=== G5a-6: port 8093 not published to host ===
docker-compose.yml exposes 8093 only on the internal network
[G5a-6] PASS

GATES PASS with DEFERRED SUB-CHECKS: G5a-5b
Phase 5a is accepted on the fast path. The deferred sub-check(s)
must run (with VERIFY_DOCKER=1) before merge to main — tracked
as a Known gap in the PR description.
```

## H-2 end-to-end contract test (separate from the gate run)

Gate G5a-3 proves the header strip set is complete; it does *not* prove
an `admin` JWT actually reaches assistant-service with `roles=["admin"]`
intact. A focused e2e test closes that loop:

- `tests/contract/test_auth_roles_e2e.py` — synthesises an HS256 JWT
  with `roles=["admin"]`, runs it through the real gateway `UserResolver`
  (JWT decode), real `_assistant_proxy.forward()`, real
  `GatewaySecretAuthMiddleware`, real assistant-service `get_user_context`,
  and asserts the resolved downstream `UserContext.roles == ["admin"]`.
  The proxy is re-pointed at an in-process FastAPI app via
  `httpx.ASGITransport` so the test is deterministic and offline.

Run: `uv run pytest tests/contract/test_auth_roles_e2e.py -v --no-cov`
→ `4 passed`.

## Known gap — G5a-5b (live docker curl)

The design doc's G5a-5 splits into two equivalent sub-checks:

1. **`[5a-5a]` — in-process middleware contract test** (6 tests via
   `TestClient`). Exercises the exact middleware code that runs in
   production. `PASS` above.
2. **`[5a-5b]` — live docker compose + curl on port 8093**. SKIPPED
   in this PR's run. The assistant-service Dockerfile requires a full
   `apt-get install build-essential` on arm64 + a pip install of the
   assistant-service package; on this host the build was still at the
   `gcc-14 aarch64` download after ~16 minutes and had to be terminated
   so the fast gates could report.

`[5a-5a]` proves the reject-unsigned-401 invariant with hard code-level
evidence — it exercises the exact middleware bytes that run in
production — so the aggregated `G5a-5 PASS` is defensible on the
**fast path**. It is not a substitute for the live-docker check
before production cutover.

**Commitment** (enforced, not just intention):
- Do **not** merge this PR (or any follow-up) to `main` without first
  running `VERIFY_DOCKER=1 bash plans/verify-phase-5a.sh` and pasting
  its stdout into the PR. `dev` is fine to land on the fast-path
  acceptance; `main` is not.
- G5a-5b is restated as G5d-1 in the design doc (Phase 5d / prod
  cutover) so the same blocker sits on the prod-deploy gate too.

## File summary

```
.env.example                                                        |   +4
docker-compose.yml                                                  |  +15 -6
packages/ai-gateway-core/pyproject.toml                             |  +11 -2
packages/ai-gateway-core/src/ai_gateway_core/auth/__init__.py       |  +14 -1
packages/ai-gateway-core/src/ai_gateway_core/auth/gateway_secret.py |  +194 (new)
packages/ai-gateway-core/src/ai_gateway_core/proxy/__init__.py      |  +24  (new)
packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py          |  +500 (new)
src/api/v1/_assistant_proxy.py                                      |  +112 -153 (-41 net)
src/api/v1/_proxy_utils.py                                          |   +93 -154 (-61 net)
src/api/v1/assistant.py                                             |   -91
apps/assistant-service/src/assistant_service/auth/__init__.py       |   +7 -1
apps/assistant-service/src/assistant_service/auth/gateway_secret_mw.py |  +90 (new)
apps/assistant-service/src/assistant_service/auth/user_context.py   |   +8 -1
apps/assistant-service/src/assistant_service/main.py                |  +25
tests/contract/__init__.py                                          |   +0 (new)
tests/contract/test_gateway_secret.py                               |  +151 (new)
tests/contract/test_gateway_secret_middleware.py                    |   +86 (new)
tests/contract/test_auth_roles_e2e.py                               |  +205 (new)
tests/proxy/test_service_proxy.py                                   |  +138 (new)
plans/verify-phase-5a.sh                                            |  +148 (new, exec)
plans/acceptance-5a.md                                              |   +…  (this file)
```

## What Phase 5a deliberately does not do

- Does **not** migrate any route to assistant-service (that is 5b/5c).
- Does **not** delete the `from assistant_service.core ...` imports in
  gateway (that is 5c after routes move).
- Does **not** reduce `src/api/v1/assistant.py` below 600 lines (5c).
- Does **not** remove `COPY apps/assistant-service/` from the gateway
  Dockerfile (5c).

Phase 5a scope is strictly the infrastructure that 5b and 5c build on top of.

---

## 北极星 verdict (post-5a)

针对 `plans/TechWhitePaper-Service-Extraction-2026-04-23.md` §二 的七条
北极星,逐条给出本 Phase 结束后的现状。**Phase 5a 的工作是为 5b/5c 打基础,
不移动路由、不删除进程内耦合、不动数据路径**,所以大部分北极星的 `gateway`
侧判定维持 ✗。这是意料之中的,也写在这里以防叙事膨胀。

白皮书 §二 item 编号:

1. **编译时解耦** — `grep -rE "from (assistant_service|knowledge_service)" src/` 为 0。
   - `gateway` → `assistant_service`: ✗ (`src/api/v1/assistant.py:37-55` + `src/main.py:1429,1440` 仍旧 import)。本 Phase 不改。
   - `gateway` → `knowledge_service`: ✓(grep 为 0,此前已成立)。
   - **本轮变动**:无。是 Phase 5c 的任务。

2. **源码单一权威** — 每个服务在仓库里只存在一份。
   - assistant-service: ✓ (`apps/assistant-service/` 是唯一源,gateway Dockerfile `COPY apps/assistant-service/` 是打包副本,不是第二份 source 仓库)。
   - knowledge-service: ? `src/services/knowledge/` 和 `apps/knowledge-service/` 的 reconcile 情况与本 Phase 无关,未改动。
   - **本轮变动**:无。

3. **启动独立** — 停掉 assistant-service / knowledge-service,gateway 仍启动,受影响路由统一返回 5xx。
   - assistant-service 挂 → gateway 启动:✗(gateway 进程里 `from assistant_service.core import ...` 会在 import 时解析。gateway 镜像仍然 `COPY apps/assistant-service/` 进去,所以 gateway 容器不需要 assistant-service 容器启动就能 import。**但 gateway 进程启动过程中 `main.py:1429` 创建 `AssistantService` 实例并跑其 lifespan-style 初始化**,该对象与 assistant-service 容器无关。严格按白皮书的判定:"运行时不共栈" 未达,但 "启动不崩" 已达。本 Phase 不触及。)
   - 受影响路由返回 5xx:`/chat/stream` ✓(本 Phase 之前的 proxy 修复里已做到);其余 22 条还在进程内。
   - **本轮变动**:无。

4. **运行时不共栈** — gateway 进程里不持有 AssistantService/ToolRegistry/MCPManager。
   - ✗ — `src/main.py:1424,1426,1440-1447` 仍把这些对象塞进 `app.state`,并在 gateway 启动时跑 MCPManager initialize_all。
   - **本轮变动**:无。Phase 5b 的任务。

5. **数据路径单一** — 同一张表的 write 入口只在一个服务。
   - ✗ — gateway 当前仍直接写 `assistant_*` 表(通过 `app.state.session_manager`)。
   - **本轮变动**:无。Phase 5b/5c 的任务。

6. **网络边界收紧** — 端口不 publish;内部调用必须带 HMAC 签名;匿名直打被 401。
   - 端口 publish:✓(本 Phase)。`docker-compose.yml` 将 assistant-service 的 `ports: ["8093:8093"]` 改为 `expose: ["8093"]`,GATE G5a-6。
   - 匿名直打被 401:✓(本 Phase)。`GatewaySecretAuthMiddleware` 在 `ASSISTANT_APP__ALLOW_ANONYMOUS=false` 下拒绝无签名请求,GATE G5a-5(sub-check `[5a-5a]`);live-docker sub-check `[5a-5b]` 是 Known gap,合入 `main` 前必须补。
   - knowledge-service 未做对等处理 — M-1 已统一 proxy 实现并共享 `GatewaySecret`,但 `_proxy_utils.py` 的 signer 用 `GATEWAY_KNOWLEDGE_SHARED_SECRET` (fallback 到 `GATEWAY_ASSISTANT_SHARED_SECRET`);knowledge-service 本身还没装 `GatewaySecretAuthMiddleware`。∴ KB 侧的 "内部调用必须带 HMAC" 未达。
   - **本轮变动**:`ASSISTANT` 方向 ✓(但 `[5a-5b]` pending);`KNOWLEDGE` 方向:共享 proxy + signer ✓,中间件 ✗。Phase 5 下一步(或 Phase 5c 结合)补 KB 侧 middleware。

7. **Auth 契约端到端** — 带 `roles=["admin"]` 的 JWT,经过 gateway proxy 后下游仍看到 `["admin"]`。
   - ✓(本 Phase)。`tests/contract/test_auth_roles_e2e.py` 合成 HS256 JWT → `UserResolver` → `_assistant_proxy.forward` → `GatewaySecretAuthMiddleware` → `get_user_context`,断言 `roles == ["admin"]`。
   - **本轮变动**:H-2 从 "看起来修了" 升级到 "有合成 JWT 端到端测试"。

### 汇总

| 北极星 | pre-5a | post-5a | 本轮变动 |
|---|---|---|---|
| 1 编译时解耦 (AS) | ✗ | ✗ | — |
| 1 编译时解耦 (KB) | ✓ | ✓ | — |
| 2 源码单一权威 | ✓ / ? | ✓ / ? | — |
| 3 启动独立 | ✗ | ✗ | — |
| 4 运行时不共栈 | ✗ | ✗ | — |
| 5 数据路径单一 | ✗ | ✗ | — |
| 6 网络边界收紧 (AS) | ✗ | ✓ (pending `[5a-5b]`) | 本 Phase 装上 `expose:` + `GatewaySecretAuthMiddleware` |
| 6 网络边界收紧 (KB) | ✗ | ✗ (signer 已到位,middleware 未到位) | signer 侧统一 |
| 7 Auth 契约端到端 (AS) | ✗ | ✓ | 本 Phase 新增 `tests/contract/test_auth_roles_e2e.py` |

**Polaris 判定:5a 仅推进了 item 6 (AS) 与 item 7 (AS)。其余五条仍 ✗。**
在 `verify-phase-5a.sh` 绿 + 以上两条推进之前,任何 commit / PR 都**不得**使用
"extracted / microservice / true isolation" 叙事;合法叙事是 "Phase 5a —
transitional proxy 硬化了边界 + auth 契约;路由迁移 5b/5c 未开始"。

本轮 **不改变** 北极星 1/2/3/4/5。它们要等 Phase 5b/5c 的路由迁移 + gateway 侧
`AssistantService`/`ToolRegistry`/`MCPManager` 持有代码整体搬走才会翻转为 ✓。
