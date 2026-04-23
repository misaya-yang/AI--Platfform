# Independent Audit — `/api/v1/assistant/chat/stream` Proxy, Current HEAD

Auditor: 第三方 senior backend reviewer / production auditor
Audit date: 2026-04-23
Branch / HEAD: `dev` @ `1c4d66e` (113 commits ahead of `origin/dev`)
Baseline: `ea5eea7` (`refactor(gateway): /api/v1/assistant/chat/stream → HTTP proxy to assistant-service`)
In-scope commits between baseline and HEAD:
- `32f1d94` fix(proxy): audit follow-up — SSE buffering, header injection, authz checks
- `e259544` fix(proxy): UnboundLocalError on body shadowing
- `fe094ed` fix(proxy): circuit breaker — no double-count + half-open probe
- `8b5d9eb` fix(proxy): drop CB time-gate — recovery waits for upstream health, not wall clock
- `1c4d66e` fix(assistant-service): configure google-vertex + dashscope-chat providers

---

## 1. Executive Summary

**这一版是"真修了几个具体 bug + 在 assistant-proxy 单文件里重写了 breaker",但它仍然只是 1 条路由的 proxy 化 + 一组局部补丁。把它叫做 "true microservice isolation" 或 "clean extraction" 是站不住的。**

总体判断:相对 `ea5eea7` 是**可观测的改善**,但**不是"核心架构问题已解决"**。核心拆分叙事夸大,且在 KB proxy 侧留下明显的 drift。

最大的 3 个风险:
1. **Auth 契约仍然缺失 `roles` 与 `user_type`。** gateway 的 `_fwd_headers` 只向 assistant-service 注入 `X-User-Id`/`X-Tenant-Id`/`X-User-Tier`,而 assistant-service 的 `tool_registry._user_has_required_permissions` **真的在用 `user.roles`** 做工具授权。结果:凡是 chat/stream 路径下的 admin/premium-role 用户,工具权限被静默降级为 `["user"]`。而且 `X-User-Type` 是 assistant-service 读取的 header,gateway **完全没把它放进 strip 列表**,所以从公网到 assistant-service 的 `X-User-Type` 可以被原样透传(属于休眠期的 smuggling 面)。
2. **`assistant.py` `chat_stream` 函数内 `return` 之后留了 ~90 行 dead code**(行 719-808)。引用了被删除的函数参数 `body: AssistantChatRequest` 和依赖 `assistant`,如果有人把早于 return 的逻辑改成条件分支,这块代码会以 `NameError` 立即炸掉。这是一个高可信度的 maintainer trap,不是"保留作参考"。
3. **`_proxy_utils.py` (KB proxy) 没有跟上修复。** 它修了 SSE chunk_size 和 header strip,但 breaker 还在老的 30s `_CB_RECOVERY` dead-zone + retry 路径上 double-count(甚至 triple-count)。所以现在 gateway 里有**两个不同语义的 breaker** —— assistant proxy 是 half-open probe,KB proxy 是 time-gate,行为漂移。

---

## 2. What Changed Since `ea5eea7`

真正触及 chat/stream 链路的文件只有 3 个:
- `src/api/v1/_assistant_proxy.py` (+90 -40)
- `src/api/v1/assistant.py` (+40 -34)
- `src/api/v1/_proxy_utils.py` (+10 -5)

`apps/assistant-service/src/assistant_service/main.py` 有 55 行变化,但是和 provider 注册相关(dashscope-chat / google-vertex),不是 auth 或 proxy 语义。

**没有被改过的关键文件**(在 `ea5eea7..HEAD` 之外):
- `apps/assistant-service/src/assistant_service/auth/user_context.py` —— auth 契约没变
- `apps/assistant-service/src/assistant_service/api/routes/chat.py` —— assistant 侧 chat/stream 没变
- `src/api/v1/knowledge.py`, `src/api/v1/kb_tools.py` —— KB 路由没变
- `Dockerfile` —— 打包配置没变,仍然把 assistant-service 源码打进 gateway 镜像

### 正向的变化
- SSE chunk_size 从 `65536` 去掉 → buffering 不再被 `ByteChunker` 拖住
- `_INJECTED_IDENTITY_HEADERS` 用 frozenset + `.lower()` 做白名单 strip,大小写绕过被堵住(但只堵了 3 个头)
- `_cb_opened` 时间窗口被废除,改成"half-open probe + 失败继续 open";解决了服务重启后 30s dead-zone 问题
- 在 retry 路径去掉了一次多余的 `_cb_fail()` 调用,assistant-service 侧 breaker 双计数问题被修好
- chat/stream 在 proxy 前先 `await request.body()`,之后把 bytes 透传给 proxy;避免了 single-consumption 导致 proxy 读空 body 的问题
- chat/stream 保留了 model permission 和 session ownership 两处 authz,这两个检查在 gateway 侧发生,不靠 assistant-service
- `/_legacy_in_process` 路由装饰器真的被删了(公网实测 404)

### 表面修补,不算真改善
- `chat_stream` 函数内 `return` 后残留 ~90 行 dead code —— 消除旧路径的是 `return` 本身,不是代码清理
- `_proxy_utils.py` 的修复不完整(见第 5 章)
- Dockerfile 仍然在 `COPY apps/assistant-service/` 然后 `pip install` 它到 gateway 镜像里;注释自己承认"A true HTTP boundary … is Phase 5; until then, the gateway image needs the assistant source bundled in"

---

## 3. Claim-by-Claim Verdict

### Claim A — chat/stream 业务逻辑跑在 assistant-service 容器,而不是 gateway

**✓ 成立**(证据充分,未做容器日志时间戳对证,但已有间接证据链)

证据:
- `src/api/v1/assistant.py:667-717` `@router.post("/chat/stream")` 结束于 `return await proxy_to_assistant_service(...)`,之后没有命中 in-process 分支的路径。
- 公网实测 `POST /api/v1/assistant/chat/stream` 且不传 `model_id`,返回了 `"Unknown model: qwen3.5-plus"` 错误事件。qwen3.5-plus 是 **assistant-service 侧 `ChatRequest` 的默认 `model_id`**(`apps/assistant-service/src/assistant_service/api/routes/chat.py:27`),gateway 侧 `AssistantConfigResponse.default_model_id` 现在是 `qwen3.6-plus`。这意味着 body 带空时 assistant-service 真的被调用、真的拿自己的默认值去查自己的 registry、真的没找到。
- 传 `model_id=qwen3.6-plus` 时,assistant-service 的 provider 配置能跑通,说明 registry 匹配。
- TTFT 实测:`time_starttransfer` ≈ 762ms(第一包),`ttft_ms` 服务端自报 2.2s–3.7s,体感与直接打 LLM 一致,没有看到明显额外的 hop 延迟。

我没做的:我没有 SSH 权限,没法去对 assistant-service 容器日志找到一条真正对应的 request_id 时间戳,所以这条证据链有 ~5% 的失真可能。需要时可以在容器里 `docker logs assistant-service | grep 93ae1ad7-cb75-4021-b603-1a806d7f2166` 做钉死。

### Claim B — 停掉 assistant-service,gateway 返回干净 502

**? 证据不足**(静态代码看起来对,但没做实际 stop 验证)

静态证据:
- `_assistant_proxy.py:216-229` 把 `httpx.ConnectError/RemoteProtocolError/PoolTimeout` 映射到 `HTTPException(502)`,`TimeoutException` → 504,其他 → 502。
- 没有发现会让 gateway worker 死掉的路径。

需要真实验证(我无法在沙盒里 SSH):
- `docker compose stop assistant-service`
- 反复 `curl -sv https://yang.misaya.online/api/v1/assistant/chat/stream`,记录 HTTP code 序列、`Retry-After` 是否出现
- `docker logs gateway` 观察是否有 traceback

### Claim C — breaker 修复后,重启即时恢复,无不合理 dead-zone

**? 证据不足,但设计层面比旧版好,也有新盲区**

静态证据:
- `_cb_check()` 去掉了 `_CB_RECOVERY = 30.0` 时间窗口,改为:`_cb_fails < _CB_THRESHOLD` 直接放行 / `_cb_probe_in_flight` True 直接 503 / 否则占用 probe 槽放行。
- 一次成功 → `_cb_fails = 0`,slot 释放。
- 一次失败 → slot 释放,counter 保留。

评价:
- **正向**:重启后第一个请求会支付一次 upstream-timeout 代价,此后要么快速恢复 (CLOSED),要么继续挡 (OPEN with no slot)。比旧 30s dead-zone 合理。
- **负向 / 新盲区**:`_do()` 在 `resp = await client.send(...)` 后只看 `status_code < 500` 决定 `_cb_success()` / `_cb_fail()`。**但 StreamingResponse 返回给 FastAPI 后,如果下游在 stream 中途崩掉,breaker 永远不会记录这次失败。** 也就是说,一个 "握手 200 + 流中间断" 的坏 upstream 永远不会触发 breaker。
- **负向 / 并发语义**:`_cb_probe_in_flight` 是 module-level 全局变量。当前 gateway `uvicorn` 启动没带 `--workers`,单 worker,asyncio cooperative,正常工作。**如果未来 scale 到 2+ workers**(这是 prod 很可能的下一步),每个 worker 都有自己的 probe slot,"唯一 probe" 保证只是 per-worker 的,整体放出的 probe 数量 = worker 数量。这是 breaker 模块级状态常见的陷阱,没有在任何 comment 里记录。
- **负向 / 4xx 算 success**:所有 `status_code < 500` 一律 `_cb_success()`。如果上游总是返回 400,breaker 永远保持 CLOSED,对"上游存在但配置错"场景无感知。虽然这是标准 breaker 语义,但值得注意。

必须做的验证(我做不了):重启 assistant-service 后打连续请求,看第一个请求耗时 + 是否立即回到低延迟。

### Claim D — SSE buffering 已被真正解决

**✓ 成立**(部分线上实测 + 代码正确)

证据:
- `_assistant_proxy.py:197` `async for chunk in resp.aiter_bytes():` 无 chunk_size,httpx 不再经过 `ByteChunker`,每个 network read 直接吐出去。
- 公网实测 `curl -N` 对 `{"message":"count 1 to 5…"}`:在 ~762ms 拿到第一包 HTTP body,之后 `thinking_delta` 事件的时间戳 (`1776929456.5443 → 1776929456.6007 → 1776929456.6868 → …`) 间隔 30-150ms,没有看到 "憋一大坨一起发" 的行为。
- response 头 `content-type: text/event-stream; charset=utf-8`、无 `content-length`、无 `content-encoding`,说明 `_clean_headers` 没把它误 compress。

注意点:
- `if cl and cl.isdigit() and int(cl) < 256 * 1024:` 是**按 content-length 判断是否 buffer**,不是按 content-type 判断。如果上游错误地给 SSE 200 响应加了 content-length(极端的 misconfig),buffering 分支会吞掉 stream。我认为风险很低,但值得用 content-type != text/event-stream 的额外条件稳一下。
- 公网响应里没有看到 `X-Accel-Buffering: no`,说明 nginx 在它前面消费掉了这个 hint,并没有转发给浏览器。这是正常 nginx 行为,不是 bug,只是 claim "前端 TTFT 不会劣化" 依赖 nginx 的 `proxy_buffering off` / X-Accel 生效,这一层我没证据。

### Claim E — header smuggling / 大小写绕过已修到位

**✗ 不成立 / 只修了一部分**

证据:
- `_INJECTED_IDENTITY_HEADERS = frozenset({"x-user-id", "x-tenant-id", "x-user-tier"})` —— **只覆盖了 gateway 自己注入的 3 个 header**。
- `apps/assistant-service/src/assistant_service/auth/user_context.py:52` 读 `X-User-Type` —— **这个 header 不在 strip 列表**,客户端送什么就给 assistant-service 送什么。
- 当前 assistant-service 代码里 `user_type` **只存不判**(就我 grep 到的 `apps/assistant-service/src/` 范围,authorization 逻辑不依赖 `user_type`)。所以是**休眠期的漏洞**,不是当前可利用的提权面。但任何未来对 `user.user_type == "admin"` 的依赖立刻变成远程可伪造。
- 同样的漏洞形态会出现在 `X-User-Email`、`X-User-Roles`、`X-Forwarded-User`、`X-Impersonated-User` 等任何 gateway 自己没显式 strip 的 header。
- 公网实测送 `x-user-id: attacker`(小写)不起作用,因为 gateway 的 streaming middleware 在到达 proxy route 之前就基于 JWT 覆盖了身份。但这**不代表 strip 做对了**;这代表 middleware 刚好兜住了。从 strip 列表本身看,设计契约不完整。

另外一点:`_fwd_headers` 用 `{k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQ}`。starlette 的 `Headers.items()` 会把每个 header 只返回一次(按首次出现的 name),所以对同名 header 的 smuggling 本身被 starlette 消化掉了。但**跨 case 的同一语义不同 name**(比如 `X-User-Email` vs `X-Email`,或 gateway 不认但 assistant-service 认的某个 header)没被覆盖。

### Claim F — auth 契约完整

**✗ 不成立**

证据:
- gateway `_fwd_headers` 只注入 `X-User-Id`/`X-Tenant-Id`/`X-User-Tier`。
- gateway `UserContext` (`src/core/auth/user_resolver.py:132`) **从 JWT 解析 `roles` 并放进 context**。
- assistant-service `UserContext.roles` 字段默认 `["user"]`(`apps/assistant-service/src/assistant_service/auth/user_context.py:22`),且 `get_user_context` **完全没从 header 读 roles**(只读 user_id / tenant_id / user_tier / user_type)。
- assistant-service `core/tools/tool_registry.py:376` `user_roles = set(user.roles or [])` —— **真的在用 roles** 做工具授权(`role:admin` / `role:premium-analyst` 等)。
- 结果:一个 JWT 里带 `roles: ["admin"]` 的用户,走 chat/stream → proxy → assistant-service → tool_registry,看到的 roles 永远是 `["user"]`。这是 **availability/UX 降级**,不是安全提权,但等价于"admin 在 chat 流里工具权限被偷偷拿掉"。
- 对称问题:`user_type` 默认 `"user"`,gateway 同样没转发,所以 assistant-service 永远看到 `"user"`;`is_anonymous` 的判断基于 user_id == "anonymous" 字符串匹配,在 proxied 请求下永远 False(因为 middleware 一定填了非 "anonymous" 的值)。这在 anon 请求上容易把 "guest" 当 "authenticated user" 处理,需要单独验证。

除此之外,**Docker 网络内伪造 `X-User-*` 的风险没被消除**:assistant-service 监听 `:8093` 在 Docker bridge 网络上,任何同网容器都能直打;`get_user_context` 只要 header 里有 `X-User-Id + X-Tenant-Id` 就无条件信任。没有 shared secret / HMAC / mTLS。只要任一 sibling 容器被攻破或有 SSRF,这一层就等于 open auth。

### Claim G — legacy in-process bypass 已真正移除

**✓ 成立**(实测)

证据:
- `grep -nE "@router\.(get|post|...)" src/api/v1/assistant.py` 找不到 `_legacy_in_process` 装饰器。
- `curl -X POST https://yang.misaya.online/api/v1/assistant/chat/stream/_legacy_in_process` → **HTTP 404**。
- 不是 schema 隐藏 (`include_in_schema=False`),是路由真的没注册。

保留问题:
- `assistant.py` 行 719-808 的 90 行 dead code(见 Claim I/Findings)留着就是**误导**。它写得像是"冷备份路径",但既不会运行也根本不可运行(引用不存在的 `body` 和 `assistant` 变量)。这是 debt,不是安全 bypass。

### Claim H — knowledge.py / kb_tools.py 是 proxy 路径,整个 KB 叙事没过度包装

**? / 部分不成立**

证据:
- `src/api/v1/knowledge.py` 和 `src/api/v1/kb_tools.py` 确实只是薄封装 → `proxy_to_kb_service`,没有 in-process fallback。✓
- 但 `_proxy_utils.py` 的实现**明显落后于** `_assistant_proxy.py`:
  - 仍然使用 `_CB_THRESHOLD, _CB_RECOVERY = 3, 30.0` 的时间窗口 breaker(assistant proxy 已去掉)
  - retry 路径上 `_cb_fail()` 被**调用 2-3 次**(一次在 except 头部 122 行,一次如果 `_do()` 内部 5xx 又被 count 一次,一次在外层 except 126 行再 count)—— 这正是 assistant proxy 刚修掉的 double-count bug
- 所以同一个仓库里,现在有两个 proxy util,behavior 漂移。对"已微服务化"的叙事 —— KB proxy 正是 assistant proxy "刚修掉那些问题" 的未修版本。

### Claim I — 拆分在部署现实里仍然不是"真拆分"

**✓ 成立**(而且是被 Dockerfile 自己明说出来的)

证据:
- `Dockerfile:57-64`:
  ```
  # Also install assistant-service package. Gateway v1 routes at src/api/v1/*.py
  # still import from ``assistant_service.core.*`` after the Phase-3 refactor.
  # A true HTTP boundary (gateway → assistant-service over :8093) is Phase 5;
  # until then, the gateway image needs the assistant source bundled in.
  COPY apps/assistant-service/ ./apps/assistant-service/
  RUN pip install --no-cache-dir ./apps/assistant-service ...
  ```
- `src/api/v1/assistant.py:37-55` 直接 `from assistant_service.core import AssistantConfig, AssistantService, ModelProvider, ModelRegistry` 等。gateway 进程启动**必须有 assistant-service 包**,没有就 import 时崩。
- `src/main.py:1427-1438`:`ASSISTANT_MODE` 默认 `"in_process"`,只有显式设置才走 remote client。chat/stream proxy 是单独一条路径,**不是通过 `assistant_client` 抽象**完成的。
- `src/api/v1/assistant.py` 里 `/chat/stream` 是**仅有的一个 proxy 化路由**;`/chat`、`/models`、`/datasets`、`/config`、`/tools`、`/policies`、`/approvals/*`、`/runs/*`、`/sessions/*`、`/artifacts/*`、`/generate-image`、`/generate-image-async`、`/image-task/*`、`/tasks/*/cancel` 等 **22+ 条路由全部还是 gateway in-process**。公网实测 `/models`、`/datasets`、`/config`、`/tools`、`/sessions` 都在 gateway 里返回,不需要 assistant-service。

结论:这一轮更准确的描述是 **"1 条热路径 proxy 化 + 一组 proxy 层 bugfix"**,而不是 "assistant service extracted"。严格意义上 Phase 5 还没开始(Dockerfile 自己承认)。

---

## 4. Findings

### Production-breaking
(none that I could prove at HEAD via available evidence; see "Unverified" for candidates)

### High

**H-1. `chat_stream` 函数 return 后残留约 90 行不可达的死代码,且引用已删除的 `body` 参数和 `assistant` 依赖**
- 位置:`src/api/v1/assistant.py:719-808`
- 证据:diff 显示 `return await proxy_to_assistant_service(...)` 在 L715-717,之后的 L720 `system_prompt_len = len(body.system_prompt)` 引用的 `body` 已经不是函数参数(函数签名是 `request: Request, user: UserContext`);`event_generator()` 内部调用 `assistant.chat_stream(...)` 的 `assistant` 也不在作用域内。
- 影响:
  1. 误导维护者:这段代码看起来像 in-process fallback,但实际上是 NameError 陷阱。
  2. 未来如果有人把 return 改成条件 return,下面的代码会立刻在 runtime 炸掉(NameError: `body`, `assistant`)。
  3. 污染 grep/IDE 索引,`body.kb_mode`、`RAGMode.AUTO`、`AssistantConfig(...)`、`assistant.chat_stream(...)` 等搜索会指向这段永远不执行的代码,降低导航信号/噪声比。
- 修复建议:**直接删除** L719-808,或者明确重构成 `if os.getenv("ASSISTANT_MODE") == "in_process": …` 兜底分支(这需要函数重新接受 `body: AssistantChatRequest` 参数)。我强烈建议删掉。

**H-2. Auth 契约不完整:roles 和 user_type 丢失,tool 授权实际被静默降级**
- 位置:`src/api/v1/_assistant_proxy.py:123-132` (`_fwd_headers`) + `apps/assistant-service/src/assistant_service/auth/user_context.py:47-70` (`get_user_context`) + `apps/assistant-service/src/assistant_service/core/tools/tool_registry.py:376` (`_user_has_required_permissions`)
- 证据:gateway 注入 `X-User-Id`/`X-Tenant-Id`/`X-User-Tier`;assistant-service 读 `X-User-Id`/`X-Tenant-Id`/`X-User-Tier`/`X-User-Type`,roles 完全不读,默认 `["user"]`;tool_registry 用 roles 做权限门闩。
- 影响:凡是 JWT 里带 `roles: ["admin", ...]` 或 `roles: ["premium-analyst"]` 之类的用户,走 chat/stream 拿到的工具集合会被降级到只包含 `role:user` 要求的部分。对 premium/enterprise tier 是明显的 UX 问题,对 admin-only 工具是功能丢失。
- 修复建议:
  1. gateway 侧在 `_fwd_headers` 里加 `"X-User-Roles": ",".join(user.roles or [])` 和 `"X-User-Type": getattr(user, "user_type", "user")`,同时把 `x-user-roles`、`x-user-type` 加进 `_INJECTED_IDENTITY_HEADERS`。
  2. assistant-service 侧 `get_user_context` 解析 `X-User-Roles` header(逗号分隔列表),填入 `UserContext.roles`。
  3. 加一个集成测试:调 `/api/v1/assistant/chat/stream` 用带 `roles:["admin"]` 的 JWT,断言 `/tools` 或对等接口返回的 `tool_list` 包含 admin-only 工具。

**H-3. Strip 列表不完整,X-User-Type / X-User-Roles / X-User-Email 在穿透端到端**
- 位置:`src/api/v1/_assistant_proxy.py:43-46`
- 证据:`_INJECTED_IDENTITY_HEADERS` 只包含 3 个 header。`X-User-Type` 在 assistant-service 侧被真实读取(即使当前不用于授权),是完全可以从公网送达的。
- 影响:当前是**休眠漏洞**,一旦 assistant-service 将来做 `if user.user_type == "admin": ...`(或 tool_registry 引入 `user_type` 判断),立刻变成远程可伪造的提权。
- 修复建议:扩展 `_INJECTED_IDENTITY_HEADERS` 到 `{"x-user-id", "x-tenant-id", "x-user-tier", "x-user-type", "x-user-roles", "x-user-email", "x-user-name"}`,并在同一 PR 里加一个 "gateway 强制 strip 识别类 header" 的单元测试。

**H-4. assistant-service 监听 :8093 在 docker bridge 网络上,无 shared-secret,任何 sibling container 直打都是 auth-trusted**
- 位置:`docker-compose.yml` + `apps/assistant-service/src/assistant_service/auth/user_context.py:47-70`
- 证据:`get_user_context` 只要 `X-User-Id + X-Tenant-Id` 非空就构造 authenticated UserContext,没有任何 network-level 或 secret-level 额外校验。
- 影响:
  1. SSRF 或任何 container 逃逸 → 直接冒充任意用户调 assistant-service。
  2. `docker exec -it knowledge-service curl http://assistant-service:8093/api/v1/assistant/chat/stream -H 'X-User-Id: ceo'` 就是完整的身份冒充。
- 修复建议(任选其一,我推荐 a+b):
  1. (a) gateway → assistant-service 加一个 `X-Gateway-Secret: <env>`,assistant-service `get_user_context` 里强制校验,不匹配 → 401。
  2. (b) assistant-service 监听 `127.0.0.1` 或 `bind_mounts` 到 unix socket(需要 compose 里调整网络)。
  3. (c) mTLS(工作量大,不建议现阶段)。

### Medium

**M-1. KB proxy (`_proxy_utils.py`) 没有跟上 assistant proxy 的修复,两个 proxy util 语义漂移**
- 位置:`src/api/v1/_proxy_utils.py:48-68, 118-133`
- 证据:breaker 还是 30s dead-zone 时间窗口;retry 路径 `_cb_fail()` 被调用 2-3 次。
- 影响:
  1. KB 服务重启后的 30s 内,所有请求会看到 503 Retry-After,即使 KB 本身已经 up。
  2. double-count 使 breaker threshold 实际上 ~1.5 次失败就跳闸。
  3. 将 assistant proxy 和 KB proxy 作为"同一套 proxy 实现"叙述是不成立的。
- 修复建议:把 `_assistant_proxy.py` 里的 breaker + retry 结构复制 / 抽到共享模块(比如 `src/api/v1/_proxy_base.py` 或 `packages/ai-gateway-core`),两个 proxy 共用。

**M-2. Breaker 不会记录"握手成功但 stream 中途断"的失败**
- 位置:`src/api/v1/_assistant_proxy.py:163-207`
- 证据:`_do()` 在 `resp = await client.send(req, stream=True)` 的 status_code 就决定了 `_cb_success/_cb_fail`。之后 `StreamingResponse(content=_stream())` 交给 starlette 去消费,`_stream` 内部抛异常不再回流到 breaker。
- 影响:一个只能接受连接但无法维持流(比如某个 Provider backend 抖动)的 upstream,永远不会触发 breaker,用户会持续看到 truncated stream。
- 修复建议:在 `_stream` 的 except 里调 `_cb_fail()`(注意:此时 coroutine 可能跨 task,对 module-level int 的原子性问题本身不恶化,可以接受)。

**M-3. `aiter_bytes()` 无 chunk_size 虽然修了 buffering,但响应 content-type 判断仍基于 content-length,不基于 media type**
- 位置:`src/api/v1/_assistant_proxy.py:183`
- 证据:`if cl and cl.isdigit() and int(cl) < 256 * 1024:` 触发 buffering 分支。
- 影响:如果某个上游错误地给 `text/event-stream` 响应带了 `content-length`(极端 misconfig,或被某层代理误加),buffering 分支会 "aread 整条 stream 再返回",彻底破坏 SSE 语义。
- 修复建议:在 buffering 条件上加 `and mt != "text/event-stream"` 显式保护。

**M-4. Module-level breaker 状态在多 worker 下不共享**
- 位置:`src/api/v1/_assistant_proxy.py:90-91, 108-119` + gateway 启动配置
- 证据:当前 `uvicorn src.main:app --host 0.0.0.0 --port 8080`,无 `--workers`,单 worker 就没问题。
- 影响:如果未来 `--workers N`,每个 worker 都有自己的 `_cb_fails` / `_cb_probe_in_flight`,"唯一 probe" 只是 per-worker 的,breaker 触发延迟也 per-worker 统计,prod 容易出现"表面上没跳闸,但 4 个 worker 各跳各的"这种奇怪行为。
- 修复建议:要么在 comment 明确"仅限单 worker";要么改用 Redis/共享 KV 存 breaker counter。

**M-5. 4xx 无条件被当作 success 关闭 breaker**
- 位置:`src/api/v1/_assistant_proxy.py:172-175`
- 证据:`if resp.status_code < 500: _cb_success()`。
- 影响:
  - 一个 "assistant-service 监听着但配置全错 → 所有请求都 400" 的情况下,breaker 永远不会跳。符合"不是 5xx 就不算 transport 失败"的经典 breaker 语义,但对"上游 healthy but broken" 无感知。
  - 更阴险:如果 gateway 某次请求的 body 解析错误,upstream 400 → breaker 归零,真正的 5xx 历史被抹掉。
- 修复建议:`_cb_success` 只在 `200 <= status < 300` 时调用,其他状态码保留 breaker state(只是不 `_cb_fail()`)。

### Low

**L-1. `_assistant_proxy.py:17` 还 import `time`,但现在 time-based breaker 已去掉,`time` 不再被使用**
- 位置:`src/api/v1/_assistant_proxy.py:17`
- 影响:死 import,linter cleanup 级别。

**L-2. Dockerfile 的 Phase-5 注释是口头承诺,没有关联 issue / PR 链接 / owner**
- 位置:`Dockerfile:57-64`
- 影响:这种 "真的拆分还要到 Phase 5" 的注释没有追踪 ID,项目演进容易忘记。
- 修复建议:注释里带 GitHub issue 号或 ADR 号。

**L-3. `/chat/stream` proxy 内在 proxy 前先 `await request.body()`,读整个 body 进内存**
- 位置:`src/api/v1/assistant.py:692`
- 影响:如果有很大的 `file_paths` JSON 或将来允许 inline base64 附件,整条 body 会在 gateway 内存里多驻留一次。当前 `enforce_rate_limit` 之后读 body,不会立刻放大,但值得知道 upstream 不再是 streaming body 了。
- 修复建议:对 body size 做硬上限(比如 4MB),超出直接 413。

**L-4. assistant.py 大量直接从 `assistant_service.core.*` 导入,使 gateway 与 assistant-service 包强耦合,任何 assistant-service 的 schema 变更都会同时打破 gateway build**
- 位置:`src/api/v1/assistant.py:37-55`
- 影响:这是 Phase-3/4 留下的现实,不是这次引入的,但要和 Claim I 一起记在技术债里。

---

## 5. Runtime Evidence

所有命令都是实跑的,输出已在会话里保留。关键片段:

### 5.1 Git 变更范围
```
$ git log --oneline ea5eea7..HEAD
1c4d66e fix(assistant-service): configure google-vertex + dashscope-chat providers
8b5d9eb fix(proxy): drop CB time-gate — recovery waits for upstream health, not wall clock
2051a8b docs(islamic-content): record live data completeness + prod deploy status
fe094ed fix(proxy): circuit breaker — no double-count + half-open probe
1a7b4a0 feat(islamic-content): add Juz, sajdahs, hizbs, search, random, context endpoints
e259544 fix(proxy): UnboundLocalError on ``body`` — inner ``body = resp.aread()`` shadows outer kwarg
32f1d94 fix(proxy): audit follow-up — SSE buffering, header injection, authz checks
```
chat/stream 链路只动了 3 个文件:`_assistant_proxy.py` +90/-40、`assistant.py` +40/-34、`_proxy_utils.py` +10/-5。

### 5.2 Legacy 路由验证
```
$ curl -sS -X POST https://yang.misaya.online/api/v1/assistant/chat/stream/_legacy_in_process -w "HTTP %{http_code}\n" -H 'content-type: application/json' -d '{}'
HTTP 404
```

### 5.3 chat/stream 真走 assistant-service 的证据
body 不带 model_id → response 里出现 `Unknown model: qwen3.5-plus`(正是 assistant-service 侧 `ChatRequest.model_id` 的默认值;gateway 侧 `/config` 返回的 default 是 `qwen3.6-plus`)。
```
data: {"event_type": "error", "data": {"code": "STREAMING_FIRST_ERROR",
       "message": "Unknown model: qwen3.5-plus", "phase": "generation_storage",
       "agent_loop_phase": "generation_storage"}, ...}
```

### 5.4 SSE TTFT / buffering 实测
```
--- curl timing ---
time_starttransfer=0.762640
time_total=3.337006
```
服务器自报:
```
{"event_type": "ttft", "data": {"ttft_ms": 2189.5, ...}}
```
text_delta 间隔 30-150ms,单个 delta `"OK"`、`"\n2\n3"` 各自独立到达,没有 chunking 积压。

### 5.5 并发两条 chat/stream
```
req1: 200 ts=2.591469
req2: 200 ts=2.591455
```
两条独立请求的 start-transfer 时间几乎相同(都是 LLM inference 段),assistant-service 单 worker + asyncio 可以并发处理。

### 5.6 非 stream 路由仍在 gateway
```
--- /models ---       200  ct=application/json
--- /datasets ---     200  ct=application/json
--- /config ---       200  ct=application/json
--- /tools ---        200  ct=application/json
--- /sessions ---     HTTP/2 200  (in-gateway, anon 可读)
```
(这些 handler 在 `src/api/v1/assistant.py` 里都是 in-process 实现,不走 proxy。)

### 5.7 代码证据:auth 契约
- gateway 注入:`_assistant_proxy.py:127-130`(X-User-Id / X-Tenant-Id / X-User-Tier)
- assistant-service 读取:`auth/user_context.py:49-52`(X-User-Id / X-Tenant-Id / X-User-Tier / X-User-Type)
- assistant-service 用 roles:`core/tools/tool_registry.py:376`(`user_roles = set(user.roles or [])`)
- gateway UserContext 有 roles:`src/core/auth/user_resolver.py:25-27, 351`(JWT 解析出 roles)

### 5.8 Dockerfile 自述的 "not a true extraction"
```
# Also install assistant-service package. Gateway v1 routes at src/api/v1/*.py
# still import from ``assistant_service.core.*`` after the Phase-3 refactor.
# A true HTTP boundary (gateway → assistant-service over :8093) is Phase 5;
# until then, the gateway image needs the assistant source bundled in.
```
`src/api/v1/assistant.py:37-55` 的 import 段正是这个耦合的具体证据。

### 5.9 未能验证的部分(honestly declared)
- `docker ps` / 容器状态、worker 数量、image size:**未验证**(沙盒没有 SSH 到 52.65.136.42 的密钥访问权限)。`Dockerfile` 静态看是 single worker;`apps/assistant-service/Dockerfile` 明确 `--workers 1`;`docker-compose.yml` 把 knowledge-service 设为 `--workers 3`。
- `docker compose stop assistant-service` → 502 / 504 的干净性:**未验证**,需要 SSH。
- breaker 重启恢复的端到端时间曲线:**未验证**,需要 SSH。
- assistant-service 容器日志里对某个 request_id 的对应记录:**未验证**,需要 SSH。

---

## 6. Resolved vs Unresolved

### 已解决
- `aiter_bytes(65536)` 的 SSE 积压 —— 已去掉 chunk_size,线上实测生效。
- 大小写绕过 smuggling(仅限 X-User-Id / X-Tenant-Id / X-User-Tier 三个 injected header)—— 已通过 `_INJECTED_IDENTITY_HEADERS` 大小写规范化修掉。
- `/_legacy_in_process` 路由已真的移除,公网 404 实测。
- breaker 30s 时间窗 dead-zone —— 已去掉,设计成 half-open probe。
- retry 路径上的 `_cb_fail()` 双计数 —— 已修(在 assistant proxy 内)。
- `UnboundLocalError` on `body` —— diff 显示已把内部 `body = await resp.aread()` rename 成 `buffered`。

### 只修了一半
- Header smuggling:只堵了 3 个 header,`X-User-Type` / `X-User-Roles` / `X-User-Email` 等仍可能绕过,当前是休眠漏洞(Finding H-3)。
- Breaker 语义:assistant 侧改了,**KB 侧没改**(Finding M-1)。

### 仍然成立 / 未解决
- auth 契约缺失 `roles` 和 `user_type`(Finding H-2)。
- assistant-service 内部网络 open auth(Finding H-4)。
- gateway image 仍打包 assistant-service 源码,import 级耦合(Claim I + Finding L-4)。
- 仅 1 条路由被 proxy 化,其余 22+ 条仍 in-process(Claim I)。

### 变形成新问题
- 老:30s dead-zone。新:握手成功后 stream 断,breaker 不计;4xx 一律 success 闭合 breaker;多 worker 下模块级状态分裂(Findings M-2 / M-4 / M-5)。
- 老:`/_legacy_in_process` 可调用。新:路由删了,但**函数体内 90 行 dead code 还在**(Finding H-1)。

---

## 7. Open Questions

1. **assistant-service 日志里是否能用 `request_id` 把 chat/stream 请求端到端对齐?** 我需要 SSH 才能验证 Claim A 的 "真的跑在另一个容器" 这条链的最后一环。间接证据(默认 model 差异)已经很强,但直接日志对证更硬。
2. **生产当前的 gateway worker 数到底是几个?** `Dockerfile` 默认单 worker,但 docker-compose.override 或 systemd 上层是否 override 未知。这直接决定 Finding M-4 的严重度。
3. **nginx 的 `proxy_buffering` 设置是什么?** 前端 TTFT 体感好不好不只取决于 gateway,还取决于 nginx。response 里 `X-Accel-Buffering` 不见了,可能是 nginx 处理完自己抹掉了。需要看 nginx 站点 conf。
4. **assistant-service 对外是不是只在 Docker 内部网络可达,还是有 port publish 暴露出宿主机 / 公网?** 如果有 publish,Finding H-4 从 "内部网络信任" 上升到 "公网直接可打"。
5. **这 113 个 ahead 的 commit 是否都打算一起 push?** 其中部分与 chat/stream 完全无关(islamic-content 等),但单 PR 叙事 vs 实际合并单位如果错位,review 成本会暴涨。

---

## 8. Recommended Follow-up

### P0(合并前必改)
- **H-1** 删掉 `assistant.py` L719-808 的 dead code(或显式改成 feature-flag 兜底分支)
- **H-2** gateway 侧补传 `X-User-Roles` / `X-User-Type`,assistant-service 侧补读;加集成测试验证 admin role 下拿到正确工具集
- **H-3** 把 `X-User-Type` / `X-User-Roles` / `X-User-Email` 加进 `_INJECTED_IDENTITY_HEADERS`;加单元测试模拟攻击

### P1(本周内)
- **H-4** gateway→assistant 加 `X-Gateway-Secret` 共享密钥,assistant-service 强制校验
- **M-1** 把 assistant proxy 的 breaker + retry 抽到共享模块,让 `_proxy_utils.py` 迁移过去,消除 drift
- **M-2** `_stream` 内 except 里补 `_cb_fail()`,覆盖 "stream 中途断" 的失败计数

### P2(下个 sprint)
- **M-3** buffering 条件加 `content-type != text/event-stream` 防御
- **M-4** 文档化 "breaker 仅限单 worker" 或改成共享 KV
- **M-5** `_cb_success` 仅在 2xx 时调用,其他状态码保留 breaker state
- **L-2** Dockerfile 里的 Phase-5 注释挂 issue/ADR 编号
- **L-3** `/chat/stream` body size 设硬上限

---

## 最不相信的 3 个 PR 叙事点

1. **"Assistant-service extracted"(真拆分)。** Dockerfile 自己承认 "A true HTTP boundary … is Phase 5;until then, the gateway image needs the assistant source bundled in"。现在更准确叫做 **transitional proxy**,不是 extraction。1/23 条路由 proxy 化 + 编译时 import 级耦合 + 同镜像打包,这三条任何一条都足以否定 "microservice" 这个词的使用门槛。

2. **"Auth contract preserved end-to-end."** 实际上 `roles` 在 proxy 边界丢失,`user_type` 在边界没 strip。tool_registry 这种**真的用 roles 授权的组件**拿到的永远是 `["user"]`。这不是"auth 契约保留",这是**隐式降权**。commit message `audit follow-up — … authz checks` 给人的暗示是 auth 整条链路都审过了,证据显示只审了 gateway 侧自己那一段。

3. **"Circuit breaker fixed"(single fix)。** 实际上**只修了 assistant proxy 那一份**,KB proxy 的 `_proxy_utils.py` 保留了 30s 时间窗 + double-count 的老实现。如果叙事是"proxy 层的 breaker 已修复",那 KB 用户重启 KB 后的 30s 内仍然会吃 503,这与叙事不一致。应当叙述为 "assistant proxy breaker 改成 half-open probe, KB proxy 待跟进"。

---

*End of audit.*
