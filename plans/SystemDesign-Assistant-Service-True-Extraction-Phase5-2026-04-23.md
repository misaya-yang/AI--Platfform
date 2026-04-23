# System Design — Assistant Service **True** Extraction (Phase 5)

- **Author**: Independent reviewer (post-`ea5eea7` audit)
- **Date**: 2026-04-23
- **Supersedes**: 部分取代 `plans/Assistant-Service-True-Isolation-Plan.md`(如有冲突以本文为准)
- **Baseline audit**: `plans/Audit-Assistant-Proxy-Current-HEAD-2026-04-23.md`
- **Target merge window**: 4 周内分 4 个 PR 合入 `dev`

---

## 0. Meta — Why this document exists and how to read it

**这份设计文档**是写给"下一个执行人"(具体来说就是 Claude Code)的。上一轮 `ea5eea7..HEAD` 的改动把 "1 条路由的 proxy 化" 包装成 "true microservice isolation / clean extraction",审计已经证明**这是不成立的叙事**。

为了防止这次再被糊弄,本文档遵循以下规则:

1. **每一个 Phase 都有一组可机器校验的 acceptance gates**。不是 "make it pass",而是 "run this exact command, expect this exact output"。一个命令没过,Phase 未完成,PR 不合。
2. **DoD(Definition of Done)中没有主观词汇**。不会出现 "make it clean / better / production-ready" 这种模糊表达。只有 "file X line Y must match regex Z" / "command W must exit 0" / "HTTP endpoint must return 502 within 3s" 这种二元判断。
3. **防欺骗清单(§11)是用户自己能跑的**。用户不用相信 agent 的叙事,自己跑 `make verify-phase-5a` 就能知道到底做了没做。
4. **如果某个 gate 暂时做不到,必须 explicit 写进 "Known gaps" 章节,而不是悄悄跳过**。

---

## 1. Requirements Gathering

### 1.1 Functional requirements

| # | 需求 | 验证点 |
|---|---|---|
| F-1 | `assistant_service` 作为**独立部署单元**,与 gateway 无编译时耦合 | `docker rmi assistant-service:* && docker compose up gateway` 能启动并对 `/health` 返回 200 |
| F-2 | gateway 不再从 `assistant_service.*` import 任何业务类 | `grep -r "from assistant_service" src/ \| grep -v _deprecated` 为空 |
| F-3 | 所有 23 条 assistant 路由(§2.2 清单)对外契约不变,但**gateway 侧**仅做 proxy + authz,**业务实现全在 assistant-service** | contract test 通过(§7) |
| F-4 | Session domain(会话、历史、artifact)归属 assistant-service,gateway 仅持有公共会话查询接口作为 proxy | gateway 代码里 `session_manager.get/create/delete` 的直接调用数 == 0 |
| F-5 | Tool registry / MCP manager 归属 assistant-service | gateway 进程启动日志不再包含 "MCP: N servers registered" |
| F-6 | KB proxy 与 assistant proxy 共用 proxy 框架,breaker / SSE / strip 语义一致 | `_proxy_utils.py` 与 `_assistant_proxy.py` 的 breaker 代码完全相同(或来自同一模块) |
| F-7 | Auth 契约 end-to-end 完整:roles / user_type 不会在 proxy 边界丢失 | contract test 覆盖 admin role → assistant-service → tool_registry,看到的是 `["admin"]` |
| F-8 | 跨服务 auth 有**内部共享密钥**(`X-Gateway-Secret`),assistant-service 拒绝无密钥的直打请求 | 用 `curl http://<host>:8093/...` 不带密钥直打 → 401 |

### 1.2 Non-functional requirements

| 指标 | 目标 | 验证方式 |
|---|---|---|
| `chat/stream` TTFT (p95) | 不劣于当前 HEAD 的 ~760ms 首字节时间(基线见审计 §5.4) | k6/bench 脚本,20 req concurrency |
| 新镜像 size: `ai-gateway` | 降 ≥ 30% vs 当前 HEAD | `docker images ai-gateway:latest --format "{{.Size}}"` 对比 |
| 新镜像 size: `assistant-service` | ≤ 当前 HEAD 的 `assistant-service` size + 5%(重构不该让它变胖) | 同上 |
| 拆分后单次 LLM 非 stream 响应额外开销 | ≤ 20ms(gateway→assistant hop) | 同网络内 bench |
| 拆分完成后 breaker 在 assistant-service 重启后 2 秒内恢复首个成功响应 | assert p50 recovery ≤ 2s | stop-start 实测(§7.4) |
| Auth 降级事故数(admin role 被识别为 user) | 0 / week | Phase 5d 后监控告警 |

### 1.3 Constraints

- 生产环境是单节点 docker-compose(EC2 `52.65.136.42`),不是 k8s —— 方案必须在 compose 里可跑
- 前端 `https://yang.misaya.online` 的契约不能变(URL path / response shape / SSE event names)
- DB 共享一个 postgres (`gateway` DB),两个服务各自管自己的 schema / table namespace
- Redis 同一个实例,按 prefix 隔离 key
- 不引入新的 infra(不上 Kafka / K8s / service mesh)
- 向后兼容:未完成拆分的 routes 保持当前行为,不因为拆分中途而 break

---

## 2. Current State (as of HEAD `1c4d66e`, 2026-04-23)

### 2.1 Coupling 证据(引用审计报告)

```
Dockerfile:57-64
  # Also install assistant-service package. Gateway v1 routes at src/api/v1/*.py
  # still import from ``assistant_service.core.*`` after the Phase-3 refactor.
  # A true HTTP boundary (gateway → assistant-service over :8093) is Phase 5;
  # until then, the gateway image needs the assistant source bundled in.
  COPY apps/assistant-service/ ./apps/assistant-service/
  RUN pip install --no-cache-dir ./apps/assistant-service …

src/api/v1/assistant.py:37-55
  from assistant_service.core import AssistantConfig, AssistantService, ModelProvider, ModelRegistry
  from assistant_service.core.assistant_service import RAGMode
  from assistant_service.core.tools.gemini_image_tool import get_gemini_image_generator
  from assistant_service.core.tools.image_callback import send_image_callback
  from assistant_service.core.tools.image_helpers import (…)
  from assistant_service.core.tools.image_watermark import apply_watermark_b64
  from assistant_service.core.tools.smart_image_generator import get_smart_image_generator
  from assistant_service.core.tools.style_presets import (…)

src/main.py:1425-1471
  app.state.model_registry = model_registry
  app.state.assistant_service = assistant_service
  app.state.assistant_gateway = assistant_service.execution_gateway
  app.state.tool_registry = tool_registry
  app.state.tenant_tool_policy = TenantToolPolicyService(...)
  app.state.tool_audit = ToolAuditService(...)
  app.state.tenant_mcp_config = TenantMCPConfigService(...)
  # MCPManager 初始化也在 gateway 进程里
```

### 2.2 Routes 现状与目标归属

Gateway `src/api/v1/assistant.py` 共 23 条 routes。标 **P** 的当前是 proxy(仅 1 条),标 **I** 的当前是 in-process(22 条),"Target" 是目标态归属。

| # | Route | Method | 现状 | Target | 备注 |
|---|---|---|---|---|---|
| 1 | `/assistant/chat` | POST | I | **proxy → as** | 非 stream chat |
| 2 | `/assistant/chat/stream` | POST | **P** | proxy → as | 已代理,保持 |
| 3 | `/assistant/models` | GET | I | **proxy → as** | model 注册表在 as 侧 |
| 4 | `/assistant/datasets` | GET | I | **proxy → as** | dataset list |
| 5 | `/assistant/config` | GET | I | **proxy → as** | assistant 级配置 |
| 6 | `/assistant/tools` | GET | I | **proxy → as** | tool_registry 归 as |
| 7 | `/assistant/policies` | GET | I | **proxy → as** | tenant policies |
| 8 | `/assistant/approvals/{id}` | POST | I | **proxy → as** | 审批 |
| 9 | `/assistant/runs/{id}` | GET | I | **proxy → as** | run 状态 |
| 10 | `/assistant/sessions` | POST | I | **proxy → as** | create session |
| 11 | `/assistant/sessions` | GET | I | **proxy → as** | list sessions |
| 12 | `/assistant/sessions/{id}` | GET | I | **proxy → as** | get session |
| 13 | `/assistant/sessions/{id}` | DELETE | I | **proxy → as** | delete session |
| 14 | `/assistant/sessions/{id}/history` | GET | I | **proxy → as** | 历史 |
| 15 | `/assistant/sessions/{id}/artifacts` | GET | I | **proxy → as** | 会话产物 |
| 16 | `/assistant/tasks/{id}/cancel` | POST | I | **proxy → as** | 取消 |
| 17 | `/assistant/artifacts/{id}` | GET | I | **proxy → as** | artifact 元信息 |
| 18 | `/assistant/artifacts` | POST | I | **proxy → as** | 创建 artifact |
| 19 | `/assistant/artifacts/{id}` | DELETE | I | **proxy → as** | 删除 artifact |
| 20 | `/assistant/artifacts/{id}/download` | GET | I | **proxy → as**(stream) | 下载 |
| 21 | `/assistant/generate-image` | POST | I | **proxy → as** | 图像生成 |
| 22 | `/assistant/generate-image-async` | POST | I | **proxy → as** | 异步图像 |
| 23 | `/assistant/image-task/{id}` | GET | I | **proxy → as** | 图像任务状态 |

**目标:Gateway 侧 `src/api/v1/assistant.py` 的最终行数 ≤ 200 行**(全是 proxy + authz 调度,没有业务),与当前 2083 行对比。

### 2.3 Missing routes on assistant-service side

当前 `apps/assistant-service/src/assistant_service/api/routes/` 只有 `chat.py`,提供 `/chat` 和 `/chat/stream`。其他 21 条路由**在 as 侧根本不存在**。Phase 5a 的主要工作之一就是把它们补上。

---

## 3. Target State — What "truly split" means (machine-verifiable)

这是**拆分完成的判定标准**。每一条都必须可以用命令在 CI / 本地验证。

### 3.1 Compile-time decoupling
```
GATE-C1: grep -rE "from assistant_service(\.|\s+import)" src/ | grep -v _deprecated_ | wc -l  → 0
GATE-C2: grep -rE "from assistant_service(\.|\s+import)" packages/ | wc -l                    → 0
GATE-C3: gateway Dockerfile 不出现 "COPY apps/assistant-service"
GATE-C4: gateway pyproject.toml dependencies 里不出现 assistant-service
```

### 3.2 Runtime decoupling
```
GATE-R1: 构建 ai-gateway 镜像时 BUILD_CONTEXT 排除 apps/assistant-service;
         `docker build -f Dockerfile -t ai-gateway:phase5-test .` 成功
GATE-R2: 启动 gateway 容器(不起 assistant-service),`/health` → 200
GATE-R3: gateway 启动日志里没有 "MCP: … registered / ToolRegistry initialized / AssistantService initialized"
GATE-R4: gateway 容器 `python -c "import assistant_service"` → ImportError
```

### 3.3 Behavioral parity
```
GATE-B1: 23 条 routes contract test 通过(§7),无回归
GATE-B2: 停掉 assistant-service,所有 23 条 routes 统一返回 502 / 503(无 500 / 崩溃)
GATE-B3: `/chat/stream` TTFT p95 ≤ baseline + 50ms
```

### 3.4 Security hardening
```
GATE-S1: assistant-service 端口 8093 只 bind 127.0.0.1(单节点)或内部网络(compose bridge);
         从宿主机公网 IP 访问 `curl http://<public-ip>:8093/…` → connection refused
GATE-S2: assistant-service 拒绝无 `X-Gateway-Secret` 的请求(401);
         带错 secret → 401;带对 secret + 完整 X-User-* → 200
GATE-S3: gateway 对 strip 列表包含 `x-user-*`(`x-user-id / x-user-tenant / x-user-tier / x-user-type / x-user-roles / x-user-email / x-user-name`),有对应 unit test
```

### 3.5 Auth contract completeness
```
GATE-A1: 发一个 JWT roles=["admin"] 的请求到 `/chat/stream`,assistant-service 日志看到 `roles=["admin"]`,
         tool_registry 授权通过 `role:admin` 要求的工具(contract test)
GATE-A2: 发一个 JWT 无 roles 的请求,assistant-service 看到 `roles=["user"]`(默认),tool_registry 拒绝 `role:admin` 工具
```

### 3.6 Proxy layer unification
```
GATE-P1: gateway 里只有一个 proxy 实现(共享模块);
         `_assistant_proxy.py` 和 `_proxy_utils.py` 要么合并,要么都 import 同一个 base 类
GATE-P2: breaker 代码 per-worker 的限制有明确 docstring / 在 multi-worker 下可用共享 store
GATE-P3: `content-type: text/event-stream` 时强制 streaming(不走 content-length buffer 分支)
```

### 3.7 Dead code 与 debt
```
GATE-D1: `src/api/v1/assistant.py` 里不存在 unreachable 代码(`ruff F841 / F401 / pyflakes` 扫描通过)
GATE-D2: `src/api/v1/assistant.py` 总行数 ≤ 200
GATE-D3: commit history 中的 "truly extracted / clean microservice" 等叙事需要在实际能通过上述 gate 的 commit 里才允许使用
```

---

## 4. High-Level Design — Target Architecture

### 4.1 Component diagram

```
                 ┌─────────────────────────────────────────────────┐
                 │              nginx / ALB (public)                │
                 └──────────────────────┬──────────────────────────┘
                                        │ https (TLS)
                                        ▼
                   ┌────────────────────────────────────┐
                   │        ai-gateway   :8080          │
                   │ ────────────────────────────────── │
                   │  * JWT / API-key / anon middleware │
                   │  * Rate limiter                    │
                   │  * Session ownership authz (light) │
                   │  * Model permission authz (light)  │
                   │  * NO business logic               │
                   │  * NO MCP / tool registry          │
                   │  * NO AssistantService import      │
                   │                                    │
                   │  proxy layer (shared):             │
                   │    - _proxy_base.ServiceProxy      │
                   │    - breaker (half-open probe)     │
                   │    - SSE stream-through            │
                   │    - header strip + inject         │
                   │    - X-Gateway-Secret sign         │
                   └───────┬───────────────┬────────────┘
                           │ http          │ http
                           │ :8093         │ :8092
                           ▼               ▼
      ┌────────────────────────────┐  ┌────────────────────────────┐
      │  assistant-service :8093   │  │  knowledge-service :8092   │
      │ ───────────────────────── │  │ ───────────────────────── │
      │  FULL chat/session/tool/  │  │  KB store / embed /        │
      │  artifact/image domain    │  │  retrieve                  │
      │                           │  │                            │
      │  * AssistantService       │  │                            │
      │  * SessionManager (owns)  │  │                            │
      │  * ToolRegistry (owns)    │  │                            │
      │  * MCPManager (owns)      │  │                            │
      │  * TenantToolPolicy       │  │                            │
      │  * ArtifactStorage client │  │                            │
      │                           │  │                            │
      │  Auth: X-User-* +         │  │                            │
      │        X-Gateway-Secret   │  │                            │
      │        shared HMAC        │  │                            │
      └──────┬──────────┬─────────┘  └────────┬───────────────────┘
             │          │                     │
             │          │                     │
             ▼          ▼                     ▼
    ┌────────────┐  ┌──────────┐     ┌────────────┐
    │  postgres  │  │  redis   │     │   qdrant   │
    │ (shared)   │  │ (shared) │     │            │
    │ schemas:   │  │ prefixes:│     │            │
    │  gateway.* │  │  gw:*    │     │            │
    │  assistant.│  │  as:*    │     │            │
    └────────────┘  └──────────┘     └────────────┘
```

### 4.2 Data flow (示例:chat/stream)

```
Browser
  │ POST /api/v1/assistant/chat/stream
  │ Authorization: Bearer <jwt>
  ▼
nginx ──▶ gateway:8080
                │ 1) Streaming middleware 验证 JWT → UserContext{roles=[admin], tier=premium, …}
                │ 2) Rate limiter (operation=assistant_chat)
                │ 3) assistant.py chat_stream route:
                │    - await request.body() 读一次
                │    - _check_model_permission(user, model_id, registry) ← 本地只保留 registry 快照
                │    - _validate_chat_session_access(...) ← proxy 到 as 的 session ownership API,不再本地 DB 查
                │ 4) proxy_base.ServiceProxy.forward(...) :
                │    - strip x-user-* / x-gateway-secret
                │    - inject X-User-Id / X-Tenant-Id / X-User-Tier
                │              / X-User-Type / X-User-Roles / X-User-Email
                │    - sign X-Gateway-Secret: HMAC(shared_secret, request_id || timestamp)
                │    - httpx.stream() 到 assistant-service:8093/api/v1/assistant/chat/stream
                ▼
assistant-service:8093
                │ 1) AuthMiddleware.verify_gateway_secret() → 401 if missing / invalid
                │ 2) user_context.get_user_context():
                │    - roles = parse(X-User-Roles) or ["user"]
                │    - user_type = X-User-Type or "user"
                │ 3) Route handler → AssistantService.chat_stream(...)
                │ 4) Stream back SSE chunks
                ▼
gateway
                │ proxy_base 原样转发 chunks
                ▼
nginx
                │ proxy_buffering off (对 text/event-stream)
                ▼
Browser (receives SSE)
```

### 4.3 API contracts

**新增 contract spec 文件**(Phase 5a 交付):
- `apps/assistant-service/docs/openapi.yaml` —— assistant-service 对外(对 gateway)的完整 OpenAPI
- `packages/ai-gateway-core/src/ai_gateway_core/contracts/assistant_service.py` —— gateway 端共享的 pydantic 模型(仅 schema,不含实现)

Contracts 原则:
1. 所有 assistant-service 端点都以 `/api/v1/assistant/*` 为 prefix —— 与 gateway 对外一致,方便 proxy 做 path 透传
2. 所有请求必须携带:`X-User-Id`, `X-Tenant-Id`, `X-User-Tier`, `X-User-Type`, `X-User-Roles`, `X-Gateway-Secret`
3. 所有响应使用 `application/json` 或 `text/event-stream`
4. 错误模型统一:`{ code: string, message: string, trace_id: string }`

### 4.4 Storage ownership

| Table / redis prefix | 所有者 | 访问方 | 说明 |
|---|---|---|---|
| `assistant_sessions`, `assistant_messages`, `assistant_artifacts`, `assistant_runs` | assistant-service | assistant-service only | gateway 通过 API 读,不直连 DB |
| `rate_limit:*` (redis) | gateway | gateway only | 网关级限流 |
| `assistant:session_cache:*` (redis) | assistant-service | assistant-service only | 会话热缓存 |
| `auth_tokens`, `api_keys`, `users`, `tenants` | gateway | gateway only | 认证域 |
| `knowledge_*` | knowledge-service | knowledge-service only | 不变 |
| `confluence_connections` 等 connector 表 | gateway(认证相关)+ assistant-service(使用)| 双方都读,但 write 只有 gateway | Phase 5c 再细化 |

### 4.5 Auth trust model(核心,必须严格)

三层:

1. **外层(Browser → gateway):** JWT / API-key / anon,已有。
2. **中层(gateway → as):** HMAC-signed `X-Gateway-Secret`:
   - 共享密钥通过环境变量 `GATEWAY_ASSISTANT_SHARED_SECRET` 注入两端
   - gateway 计算 `HMAC-SHA256(secret, f"{request_id}:{epoch_millis}")`,发到 `X-Gateway-Secret` 头
   - as 侧同步时钟(compose 内同一主机 OK),容忍 ±60s 漂移;超过拒
   - 防回放:`X-Request-Id` 用 UUIDv4;as 侧 redis 里存 5 分钟 TTL,重复 request_id 拒
3. **内层(as → DB / KB / redis):** 依赖基础设施自身的 DSN / password,不跨信任边界。

明确禁止:
- as 监听公网 IP(必须 bind `127.0.0.1` 或 docker internal network)
- as 信任匿名请求(`ASSISTANT_APP__ALLOW_ANONYMOUS` 在 prod 必须 `false`)

---

## 5. Deep Dive

### 5.1 Proxy framework(共享模块)

新建 `packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py`:

```python
# 伪代码 / spec
class ServiceProxy:
    def __init__(
        self,
        name: str,                          # "assistant-service"
        base_url: str,
        shared_secret: str,
        timeout: httpx.Timeout,
        limits: httpx.Limits,
        breaker: CircuitBreaker,            # 注入,而非模块级 global
    ): ...

    async def forward(
        self,
        request: Request,
        user: UserContext,
        *,
        upstream_path: str,
        body: bytes | None = None,
    ) -> Response: ...

    # 内部
    def _strip_inbound_headers(self, h) -> dict: ...
    def _inject_identity(self, h, user) -> dict: ...
    def _sign_gateway_secret(self, request_id: str) -> str: ...
    def _maybe_buffer_or_stream(self, resp) -> Response: ...
    # 关键:if content-type == "text/event-stream": force stream
```

`_assistant_proxy.py` / `_proxy_utils.py` 都变成 8-10 行胶水,仅构造 ServiceProxy 实例并委派。

**Breaker 抽成类**:
- CLOSED / OPEN 两状态 + half-open probe slot
- 非 2xx 都算失败(不再把 4xx 当 success)
- 流中途断也算失败(通过 stream wrapper 在 except 里 `_on_failure()`)
- `counter_store` 可注入:`InMemoryCounter`(单 worker)/ `RedisCounter`(多 worker 共享)

### 5.2 Session ownership 搬迁

当前 `app.state.session_manager` 在 gateway 侧,`apps/assistant-service/src/assistant_service/` 下**没有** SessionManager。

迁移:
1. 将 gateway 的 `src/services/.../session_manager.py`(或等价位置)**物理移动**到 `apps/assistant-service/src/assistant_service/persistence/session_manager.py`
2. gateway 删除该模块;在 gateway 里对 session 的所有操作改走 `assistant_service_client.sessions.*`
3. DB schema 保留在同一个 `gateway` DB,但 table 前缀明确为 `assistant_*`
4. Gateway 需要的 session ownership 轻校验 —— **不直连 DB**,调 as 的 `GET /api/v1/assistant/sessions/{id}/owner` → 返回 `{user_id, tenant_id}` 供 gateway 比对

### 5.3 Tool registry / MCP 搬迁

现状:MCPManager 在 gateway 启动时初始化并持有。

迁移:
1. MCPManager 初始化代码从 `src/main.py:1440-1454` 整段搬到 `apps/assistant-service/src/assistant_service/main.py` 的 lifespan 里
2. `tool_registry`、`tenant_tool_policy`、`tool_audit`、`tenant_mcp_config` 同理
3. Gateway 里任何对 `app.state.mcp_manager / tool_registry / …` 的引用必须变成:
   - 删除,或
   - 改成 `assistant_service_client.tools.list(...)` 等代理
4. 网关侧 `/assistant/tools`、`/assistant/policies` 等路由变成纯 proxy

### 5.4 Deploy 拓扑

`docker-compose.yml` 改动:
```yaml
# before
assistant-service:
  ports: ["8093:8093"]   # ← 公网 IP 上 0.0.0.0:8093,高风险

# after
assistant-service:
  # 不 expose 宿主机端口;仅在 ai-gateway-net 内可达
  expose: ["8093"]
  environment:
    ASSISTANT_APP__ALLOW_ANONYMOUS: "false"
    GATEWAY_ASSISTANT_SHARED_SECRET: "${GATEWAY_ASSISTANT_SHARED_SECRET:?required}"

gateway:
  environment:
    ASSISTANT_SERVICE_URL: "http://assistant-service:8093"
    GATEWAY_ASSISTANT_SHARED_SECRET: "${GATEWAY_ASSISTANT_SHARED_SECRET:?required}"
```

生产 `.env` 需要注入 `GATEWAY_ASSISTANT_SHARED_SECRET=<32-byte-hex>`。部署 checklist 里增加该变量。

### 5.5 Error model

统一错误 envelope:
```json
{
  "code": "ASSISTANT_UNAVAILABLE" | "AUTH_DENIED" | "SESSION_NOT_FOUND" | ...,
  "message": "human-readable",
  "trace_id": "<uuid>",
  "retry_after_ms": 2000  // 可选,仅限 5xx
}
```

HTTP status:
- Gateway → client:
  - `4xx` from as → 原样透传
  - `5xx` from as → gateway 重写为 502 / 503 / 504 并保留 code+trace_id
  - breaker OPEN → 503 + Retry-After
  - as 连不上 → 502
  - as 超时 → 504

---

## 6. Phased Execution Plan

每个 Phase 一个独立 PR,**必须**按顺序合入。

### Phase 5a — Contract + Proxy framework + Auth hardening (P0)
**目标:打好基础,不动业务逻辑,不做路由迁移。**

**Tasks**
1. 新建 `packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py`,内含 `ServiceProxy` + `CircuitBreaker` + `InMemoryCounter` + `RedisCounter`
2. 重构 `src/api/v1/_assistant_proxy.py` → 10 行胶水,委派 `ServiceProxy`
3. 重构 `src/api/v1/_proxy_utils.py` → 同上
4. `_INJECTED_IDENTITY_HEADERS` 扩展为 `{x-user-id, x-tenant-id, x-user-tier, x-user-type, x-user-roles, x-user-email, x-user-name}`
5. `_fwd_headers` 扩展为注入 Id/Tenant/Tier/Type/Roles(roles 逗号分隔)
6. assistant-service `user_context.get_user_context` 解析 `X-User-Roles` header
7. 共享密钥:`packages/ai-gateway-core/.../gateway_secret.py`,提供 `sign()` / `verify()`
8. assistant-service 新中间件 `GatewaySecretAuthMiddleware`;`ASSISTANT_APP__ALLOW_ANONYMOUS` prod 必须 false
9. **删除** `src/api/v1/assistant.py:719-808` 的 dead code
10. `docker-compose.yml` 去掉 `ports: ["8093:8093"]`,改 `expose`,加 secret env

**Acceptance gates**
```bash
# G5a-1: 共享 proxy 模块存在且被两侧使用
test -f packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py
grep -q "from ai_gateway_core.proxy" src/api/v1/_assistant_proxy.py
grep -q "from ai_gateway_core.proxy" src/api/v1/_proxy_utils.py

# G5a-2: dead code 已删
python -c "
import ast, sys
tree = ast.parse(open('src/api/v1/assistant.py').read())
for fn in ast.walk(tree):
    if isinstance(fn, ast.FunctionDef) and fn.name == 'chat_stream':
        # 查找函数体里 Return 之后还有 statement
        body = fn.body
        for i, s in enumerate(body):
            if isinstance(s, ast.Return) and i < len(body) - 1:
                sys.exit('dead code after return found')
print('ok')
"

# G5a-3: strip 列表齐全
python -c "
from src.api.v1._assistant_proxy import _INJECTED_IDENTITY_HEADERS as H
required = {'x-user-id','x-tenant-id','x-user-tier','x-user-type','x-user-roles','x-user-email','x-user-name'}
assert required.issubset(H), f'missing: {required - H}'
"

# G5a-4: 共享密钥 unit test
pytest tests/contract/test_gateway_secret.py -v   # 新增

# G5a-5: assistant-service 拒绝无密钥调用
docker compose up -d assistant-service
curl -sSo /dev/null -w "%{http_code}\n" http://localhost:8093/api/v1/assistant/chat \
  -H 'content-type: application/json' -H 'x-user-id: u1' -H 'x-tenant-id: t1' -d '{}'
# expected: 401
docker compose down

# G5a-6: assistant-service 不再暴露公网端口
! nc -zv <public-ip> 8093
```

**Rollback**: revert 单个 PR。对现有用户零影响(没有 route 迁移)。

**Est**: 3-4 天

---

### Phase 5b — 迁移 CRUD routes(非 stream / 幂等)
**目标:迁 16 条非 stream、非高频的 routes(见 §2.2 的 #3-9, #10-15, #17-19, #23)。**

**Tasks**
1. 在 `apps/assistant-service/src/assistant_service/api/routes/` 下新建 `sessions.py`, `models.py`, `datasets.py`, `config.py`, `tools.py`, `policies.py`, `approvals.py`, `runs.py`, `artifacts.py`, `image_tasks.py`,分别实现对应 routes
2. 业务代码从 gateway `src/api/v1/assistant.py` 物理搬到 `apps/assistant-service/src/assistant_service/core/services/*.py`
3. Gateway 侧这些路由改为 `proxy.forward(request, user, upstream_path=<path>)`
4. Gateway 侧 `app.state.session_manager` 的**直接调用**全部删除,改走 proxy
5. MCPManager 初始化从 gateway 搬到 as
6. Tool registry / tenant policies 初始化同上

**Acceptance gates**
```bash
# G5b-1: gateway assistant.py 行数大幅下降
test $(wc -l < src/api/v1/assistant.py) -le 600   # Phase 5c 再压到 200

# G5b-2: 所有 16 条 routes 都有 as 侧实现
for path in models datasets config tools policies sessions runs ; do
  curl -sSf -H "X-Gateway-Secret: $(gen-secret)" \
    -H "X-User-Id: test-u" -H "X-Tenant-Id: test-t" \
    -H "X-User-Roles: user" -H "X-User-Tier: normal" \
    http://localhost:8093/api/v1/assistant/$path > /dev/null
done

# G5b-3: Contract tests 全绿(§7)
pytest tests/contract/test_assistant_routes.py -v

# G5b-4: gateway 内部不再持有 MCPManager / ToolRegistry
! grep -n "MCPManager\|tool_registry\|tenant_tool_policy" src/main.py

# G5b-5: session ownership 检查走 proxy,不直接 DB
! grep -n "session_manager\." src/api/v1/assistant.py
```

**Rollback**: 每一条 route 做 feature flag `ASSISTANT_ROUTE_<NAME>_PROXIED=true/false`,flag 关回 false 即回退。

**Est**: 1.5 周

---

### Phase 5c — 迁移流式 / 重路由 + 清理 gateway 代码
**目标:迁 `/chat`(非 stream)+ `/artifacts/{id}/download`(stream)+ `/generate-image*` + dead-import cleanup。把 gateway 压到 <200 行。**

**Tasks**
1. 迁剩余 7 条 routes
2. **物理删除** gateway 仓库中所有 `from assistant_service.*` import
3. **物理删除** gateway `src/` 下所有 `AssistantService / ToolRegistry / MCPManager` 的持有代码
4. gateway pyproject.toml 移除 assistant-service 依赖
5. gateway Dockerfile 移除 `COPY apps/assistant-service/ && RUN pip install …`

**Acceptance gates**
```bash
# G5c-1: 无 assistant_service import
test $(grep -rE "from assistant_service(\.|\s+import)" src/ packages/ | wc -l) -eq 0

# G5c-2: gateway 镜像不含 assistant-service 包
docker build -t ai-gateway:phase5c .
docker run --rm ai-gateway:phase5c python -c "import assistant_service" 2>&1 | grep -q "ModuleNotFoundError"

# G5c-3: 停掉 assistant-service 时 gateway 仍可启动(所有 assistant routes 返回 502/503)
docker compose up -d --no-deps gateway    # 不带 assistant-service
sleep 10
test "$(curl -sSo /dev/null -w '%{http_code}' http://localhost:8080/health)" = "200"
test "$(curl -sSo /dev/null -w '%{http_code}' -X POST http://localhost:8080/api/v1/assistant/chat -d '{}' -H 'content-type: application/json')" = "502"

# G5c-4: gateway 镜像 size 下降
before=$(docker images ai-gateway:pre-phase5 --format "{{.Size}}")
after=$(docker images ai-gateway:phase5c --format "{{.Size}}")
# after <= before * 0.7

# G5c-5: gateway assistant.py <= 200 行
test $(wc -l < src/api/v1/assistant.py) -le 200
```

**Rollback**: revert PR(Phase 5b 已提供 feature flag 的 route,此阶段只拆 import)。

**Est**: 1 周

---

### Phase 5d — 生产切换 + 监控 + auth 端到端
**目标:生产部署新架构,打开监控,完成端到端 auth contract test。**

**Tasks**
1. Prod `.env` 注入 `GATEWAY_ASSISTANT_SHARED_SECRET`
2. Rolling deploy:先起新 assistant-service,再起新 gateway
3. 接 Prometheus metric:`proxy_requests_total{service="assistant-service", status=…}`、`proxy_breaker_state{service,...}`、`proxy_ttfb_ms` 直方图
4. 加 log metric:gateway 和 as 各打印 `request_id / user_id / tenant_id / roles / tier`,便于端到端 trace 对齐
5. E2E test suite (§7.5)

**Acceptance gates**
```bash
# G5d-1: prod 上公网打 8093 失败
! curl -sS --max-time 3 http://52.65.136.42:8093/health

# G5d-2: prod 上端到端 admin role 契约
# (用测试 JWT, roles=["admin"] )
RESP=$(curl -sSN -X POST https://yang.misaya.online/api/v1/assistant/chat/stream \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H 'content-type: application/json' \
  -d '{"message":"list admin tools","model_id":"qwen3.6-plus"}')
# 用 contract test 校验响应事件里 tool 列表包含 admin-only 工具

# G5d-3: stop-start 恢复时间 <= 2s
ssh prod 'docker stop assistant-service; sleep 5; docker start assistant-service'
# 另一端起 10 个并发请求,记录第一个成功响应时间
python scripts/measure_recovery.py --target chat/stream --expected-p50-ms 2000

# G5d-4: TTFT 未退化
python scripts/bench_ttft.py --target chat/stream --samples 100 --p95-ms 810
# 810 = 762 baseline + 50 预算

# G5d-5: Metrics 端点可见
curl -sSf https://yang.misaya.online/metrics | grep -E "proxy_requests_total|proxy_breaker_state|proxy_ttfb_ms"
```

**Rollback**: 每个服务保留上一版镜像 tag,compose 把 image 换回即可。注意 `GATEWAY_ASSISTANT_SHARED_SECRET` 是新增 env,旧版 gateway 不读,向后兼容。

**Est**: 3-4 天(含 bake time)

---

## 7. Testing Strategy

### 7.1 Contract tests(新增目录:`tests/contract/`)
- `test_assistant_routes.py`: 每条 route 分别测试"gateway proxy 透传正确 / headers 注入正确 / error 映射正确"
- `test_gateway_secret.py`: sign / verify / replay 防护
- `test_auth_contract.py`: roles/user_type 端到端保留;admin/premium/user 三档 tier 各测一遍

### 7.2 Unit tests
- `packages/ai-gateway-core/tests/proxy/test_service_proxy.py`: breaker 状态机、header strip、SSE stream、content-type 判定
- `tests/unit/test_strip_headers.py`: 大小写 / 多值 / 畸形 headers 边界

### 7.3 Integration tests(本地 docker compose)
- 起全栈 → 跑 pytest `tests/integration/`
- 覆盖:chat/stream、session CRUD、tool list(admin vs user)、artifact download

### 7.4 Chaos tests
- `scripts/chaos_stop_assistant.sh`: 停 assistant-service,拉 30s 的 502 曲线,再起来,测 recovery time
- 期望:breaker 在 5s 内由 OPEN → CLOSED,无 30s dead zone

### 7.5 E2E(prod-like)
- `scripts/e2e_assistant.py`: 带真实 JWT,打 `yang.misaya.online`,断言事件序列 + TTFT + 错误码映射

### 7.6 Anti-regression for audit findings
每个 audit 里的 finding 做成一个回归测试:
- H-1: `ruff check src/api/v1/assistant.py --select F841,F401`
- H-2: contract test admin role → tool list 包含 admin-only
- H-3: unit test `_INJECTED_IDENTITY_HEADERS` 完整性
- H-4: chaos test 模拟 container SSRF,期望 401
- M-1: grep 保证只剩一个 proxy 实现
- M-2: breaker 覆盖 stream-mid-failure
- M-3: content-type == event-stream 时强制 stream 分支
- M-4: breaker docstring 明确多 worker 限制
- M-5: 4xx 不再 close breaker

---

## 8. Scale and Reliability

### 8.1 Load
- 当前 prod:~100 请求 / 分钟(估算),chat/stream 集中在峰值
- 拆分后 gateway → as 多一次内网 hop,TCP 往返延迟 <1ms(docker bridge)
- LLM inference 占总延迟 >95%,hop 开销可忽略

### 8.2 Scaling
- assistant-service 水平扩展:多副本 + nginx upstream / docker-compose replicas(后续 k8s 迁移时自然 fit)
- 前提:SessionManager 所有状态在 DB / Redis,无进程内 state;MCP connection 要么连 stateless 服务、要么每个 worker 自己维持连接
- gateway 水平扩展:breaker 必须用 RedisCounter 共享状态(在 Phase 5a 已经预留接口)

### 8.3 Failover
- assistant-service 挂:breaker OPEN,所有 assistant routes 502/503,其他 gateway 功能正常(KB、auth、files 不受影响)
- knowledge-service 挂:已有独立 breaker(`_proxy_utils`)
- gateway 挂:nginx 502,assistant-service 作为纯内网服务不可从外访问(符合安全)

### 8.4 Monitoring
- Gateway:`proxy_requests_total`, `proxy_breaker_state`, `proxy_ttfb_ms`, `proxy_latency_ms`, `auth_failures_total`
- As:`chat_stream_duration_ms`, `tool_authorization_denied_total`, `mcp_tool_invocations_total`
- 告警阈值:
  - `proxy_breaker_state == OPEN` 超过 60s → PagerDuty
  - `auth_failures_total{reason="invalid_gateway_secret"} > 0` → 安全告警(可能是攻击)

---

## 9. Trade-offs

| 决策 | 选了什么 | 放弃了什么 | 理由 |
|---|---|---|---|
| DB 共享 vs 独立 | 共享 postgres 实例,不同 schema | 完全独立实例 | 工作量大 + 一机部署 + 独立实例 TCO ↑;可在 k8s 阶段再拆 |
| Session 归属 | 完全归 assistant-service | 保留 gateway 一份 | 避免双写一致性问题;gateway 变纯 proxy 更符合 extraction 目标 |
| Auth 层 | 共享 HMAC 密钥 | mTLS / SPIFFE | mTLS 工作量大,HMAC 足够覆盖内部网络 + SSRF 场景 |
| Proxy 抽象位置 | `packages/ai-gateway-core` | gateway 内部 `src/core/proxy/` | core 是 workspace package,其他服务也可复用 |
| 分 4 个 PR | 小步快跑 | 大爆炸 PR | 上一轮 "ea5eea7" 就是大 PR + 不完整收尾的反面案例 |
| Breaker 实现 | 先 In-memory,接口留给 Redis | 一次到位 Redis | 当前单 worker,Redis 引入新依赖时机点;留接口保证后续换不需要改业务代码 |
| Session ownership 校验 | gateway 调 as 的 owner-check API(一次额外 HTTP) | gateway 直接查 DB | 直接查 DB 意味着 gateway 仍然耦合 assistant schema,不算真正拆分 |

---

## 10. Risks

| Risk | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 搬迁过程中某条非 stream route 的隐式依赖(如共享内存缓存)被破坏 | 中 | 中 | Phase 5b 的 feature flag / contract test 每条 route 单独验证 |
| 前端依赖了当前 response 的某个 gateway-internal 字段(如 `gateway_decision` 事件) | 中 | 高 | 迁移前 snapshot 前端对事件的消费点;保持事件名不变 |
| `GATEWAY_ASSISTANT_SHARED_SECRET` 泄露(进 git / 日志) | 低 | 高 | 加到 `.gitignore` + pre-commit hook 检测 32-byte hex / 走 secret manager |
| HMAC 时钟漂移(容器时钟)拒绝合法请求 | 低 | 中 | ±60s 容忍窗口 + 监控 `auth_failures{reason=clock_skew}` |
| 拆分后 prod TTFT 劣化超预算 | 低 | 中 | Phase 5d G5d-4 硬 gate;超过回滚 |
| Claude Code 仍然糊弄:commit 写 "extracted" 实际只改了一半 | 中 | 高 | §11 的防欺骗验收清单,用户可自行 `bash plans/verify-phase-5x.sh` |

---

## 11. Anti-Fraud Verification Checklist(防欺骗)

**这一章是写给用户自己的。** 不要信 Claude Code 说 "Phase 5x 完成",跑这些命令判断。

### 11.1 交付物清单(每个 Phase PR 必须包含)
- [ ] 修改的源码
- [ ] 新增 / 更新的 test
- [ ] `plans/verify-phase-5<x>.sh` 脚本(见 §11.2)
- [ ] `plans/acceptance-5<x>.md` —— 对照 §6 的 acceptance gates,逐条给出命令 + 实测输出截图 / 文本

### 11.2 自己跑验证(用户侧,不依赖 agent)
示例 `verify-phase-5a.sh`:
```bash
#!/usr/bin/env bash
set -eo pipefail

echo "=== G5a-1: 共享 proxy 模块存在 ==="
test -f packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py || { echo FAIL; exit 1; }
grep -q "from ai_gateway_core.proxy" src/api/v1/_assistant_proxy.py  || { echo FAIL; exit 1; }
grep -q "from ai_gateway_core.proxy" src/api/v1/_proxy_utils.py      || { echo FAIL; exit 1; }
echo OK

echo "=== G5a-2: dead code 已删 ==="
python3 -c "
import ast, sys
tree = ast.parse(open('src/api/v1/assistant.py').read())
for fn in ast.walk(tree):
    if isinstance(fn, ast.FunctionDef) and fn.name == 'chat_stream':
        body = fn.body
        for i, s in enumerate(body):
            if isinstance(s, ast.Return) and i < len(body) - 1:
                sys.exit('dead code after return found')
print('ok')
"

echo "=== G5a-3: strip 列表齐全 ==="
python3 - <<'PY'
from src.api.v1._assistant_proxy import _INJECTED_IDENTITY_HEADERS as H
required = {'x-user-id','x-tenant-id','x-user-tier','x-user-type','x-user-roles','x-user-email','x-user-name'}
assert required.issubset(H), f'missing: {required - H}'
print('ok')
PY

echo "=== G5a-5: 无密钥调 as 返回 401 ==="
docker compose up -d assistant-service
sleep 5
code=$(curl -sSo /dev/null -w "%{http_code}" http://localhost:8093/api/v1/assistant/chat \
  -H 'content-type: application/json' -H 'x-user-id: u1' -H 'x-tenant-id: t1' -d '{}')
test "$code" = "401" || { echo "expected 401 got $code"; exit 1; }
docker compose down

echo "=== G5a-6: 公网 8093 不通 ==="
! nc -zv -w 3 <PUBLIC-IP> 8093 2>&1 || { echo "port 8093 still public"; exit 1; }

echo "ALL GREEN — Phase 5a accepted"
```
类似 `verify-phase-5b.sh / 5c.sh / 5d.sh`,每个 Phase 落一个。

### 11.3 烟雾识别(主观判断,但有迹可循)
以下信号 = 大概率 agent 又在糊弄:
- PR 描述里出现 "truly extracted / clean microservice / production-ready",但 §11.2 脚本没跑通
- commit message 说 "Phase 5 complete",但 `git diff` 只改了 2-3 个文件,<200 行
- Acceptance gates 被改写(比如把 `== 0` 偷偷改成 `<= 5`)
- 新增 test 文件但全是 trivial assertion(`assert True` / `assert result is not None`)—— 用 mutation test 抓
- `scripts/verify-phase-*.sh` 里用 `|| true` / `2>/dev/null` 消错

如果观察到任意一条,请求 agent 拿 §11.2 的脚本**原样不动**执行一遍。

---

## 12. Open Questions(留给 Claude Code 自己先回答,不要猜)

1. `app.state.assistant_client`(`src/main.py:1431-1439`)的 Protocol-based 抽象现在是否已经可以作为 Phase 5b 迁移的起点?如可,是否 Phase 5a 就应该先把它接到共享 ServiceProxy?
2. 前端代码里有没有直接消费 `gateway_decision` / `run_started` 等事件的固定逻辑?迁移到 as 后事件名是否保持?(需要 @frontend 同步确认)
3. `confluence_tools` 注册在 gateway 侧,但 tool_registry 要搬到 as,confluence 连接状态查询怎么跨服务?(倾向 as 通过 API 读 gateway 的 connector DB)
4. `artifact_storage`(S3 / MinIO)是 shared,gateway 需不需要保留对 bucket 的读权限?(倾向:仅 as 写,gateway 只发 presigned URL)
5. Prod 的 nginx conf `proxy_buffering` 现在是否对 `/api/v1/assistant/chat/stream` 显式 off?如果不是,Phase 5d 要同步改 nginx

---

## 13. Deliverables Summary

| Phase | PR | 主要交付 | 合并 gate |
|---|---|---|---|
| 5a | `feat(proxy): shared ServiceProxy + gateway HMAC auth` | 共享 proxy / 密钥 / dead code 清理 | G5a-1..6 全绿 |
| 5b | `feat(assistant-service): migrate CRUD routes (non-stream)` | 16 条 routes 迁移 + session/tool 搬迁 | G5b-1..5 全绿 |
| 5c | `feat(assistant-service): complete route migration + drop in-process imports` | 剩余 routes + 代码清理 + 镜像瘦身 | G5c-1..5 全绿 |
| 5d | `chore(prod): cutover to true extraction + observability` | 部署切换 + 监控 + E2E auth | G5d-1..5 全绿 |

每个 PR 描述必须贴上:
- Acceptance gates 实测输出(命令 + stdout/stderr)
- `scripts/verify-phase-5x.sh` 退出码
- audit 文档中对应 Finding 的回归测试 link

---

## Appendix A — Commands the executing agent MUST NOT run

以下命令会让验证失效或不可信,禁止在 Phase 5 的任一 PR 中使用:
- `|| true` 或 `2>/dev/null` 包裹 acceptance gate 命令
- `pytest -k '...'` 只跑部分测试作为 gate
- 修改 `plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md` 本身来放松 gate(需用户 review 本文档修改)
- 在 commit message 中声明 "Phase X complete" 但 `verify-phase-X.sh` 没有绿

## Appendix B — Route-by-route migration worksheet

见 §2.2 的 23 行表格,每条在 Phase 5b/5c 完成后必须能 tick。

## Appendix C — If an acceptance gate is legitimately blocked

允许的妥协形式:
- 在 PR 描述里新增 "Known gap" 段落
- 明确写出哪一条 gate 没过、为什么、多久内补
- 不允许:把 gate 从本文件删掉或弱化;不允许把未过的 gate 悄悄声明为 "passed"

---

*End of design.*
