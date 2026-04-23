# 技术白皮书 — AI Gateway 服务拆分

**To**: 下一位执行人(Claude Code 或其他)
**From**: 独立第三方审计(2026-04-23)
**Re**: assistant-service 与 knowledge-service 的**真**拆分
**Posture**: 你有判断力 —— 这份文档只给原则、红线、验收,不给步骤。

---

## 一、一页纸诊断

当前 AI Gateway 对外宣称"assistant/knowledge 已微服务化"。**这个叙事不成立。**

两条证据链:

1. **assistant-service**:Dockerfile 自己承认 *"until then, the gateway image needs the assistant source bundled in"*(L57-64);gateway `src/api/v1/assistant.py:37-55` 直接从 `assistant_service.core.*` import 业务类;23 条 assistant routes 只有 `/chat/stream` 一条走 HTTP boundary,其余 22 条全在 gateway 进程内跑。

2. **knowledge-service**:gateway 里 `src/services/knowledge/` 有 52 个文件 / 32234 行是一整套完整的 KB 实现;`apps/knowledge-service/` 里另有一份 32434 行,两份**已经 diverge 约 200 行**;gateway 启动时同时**实例化 KnowledgeService 并启动 KnowledgeWorker**,而 knowledge-service 容器自己也启动 worker —— 两个 worker 连同一个 postgres / 同一张 `documents` 表。Confluence 增量同步完全绕过 knowledge-service 容器,在 gateway 进程内直写 qdrant。

准确定位:**两者都处于"路由层部分 proxy 化 + 运行时仍在进程内"的过渡态**,不是微服务拆分。

---

## 二、北极星 — "真拆分" 的定义

判断拆分完成的唯一标准是这七条是否同时为真。七条都是二元判断,不是主观描述。

1. **编译时解耦**:gateway 仓库里 `grep -rE "from (assistant_service|knowledge_service)" src/` 为 0。
2. **源码单一权威**:每一个服务在仓库里只存在一份代码。没有 gateway 里另留一份 fork。
3. **启动独立**:把 assistant-service 或 knowledge-service 容器 kill 掉,gateway 仍然启动成功,受影响的路由统一返回 5xx(不是崩溃)。
4. **运行时不共栈**:gateway 进程里不持有这两个服务的业务对象(`KnowledgeService`、`KnowledgeWorker`、`AssistantService`、`ToolRegistry`、`MCPManager`),也不跑它们的后台任务(ingestion worker、MCP connection、confluence scheduler)。
5. **数据路径单一**:同一张表的 write 入口只有一个服务。`documents` 只由 knowledge-service 写,`assistant_sessions` 只由 assistant-service 写。
6. **网络边界收紧**:两个服务的端口不 publish 到宿主机;gateway → 下游服务必须带 HMAC 签名的共享密钥;匿名直打服务端口被 401 拒绝。
7. **Auth 契约端到端**:一个带 `roles=["admin"]` 的 JWT,经 gateway proxy 到达下游后,下游看到的 `user.roles` 仍然是 `["admin"]`(不是静默降级为 `["user"]`)。

**如果这七条同时绿,才允许用 "extracted / microservice / true isolation" 这种叙事。** 否则用 "transitional proxy / partial extraction" 描述现状。

---

## 三、指导原则 — 留给 Claude Code 的判断空间

这一节给你原则,**不给步骤**。你可以自由决定拆的顺序、工具、抽象层次。

### P1 · 两个服务一套 proxy
Gateway 现在有 `_assistant_proxy.py` 和 `_proxy_utils.py` 两份 proxy 实现,breaker / strip / SSE 语义不同步。任何新一轮拆分工作**必须先把它们统一**到一个共享 proxy 基类,否则每次 bug 修在一边另一边就漂移。把它抽到 `packages/ai-gateway-core` 或 `src/core/proxy/` 都行,决定权在你。

### P2 · 先合并代码,再拆进程
**反直觉但正确**:KB 的两份 diverged 代码必须**先合并到 apps/knowledge-service 那份为唯一源**,然后再删 gateway 里那份。先拆进程再合代码会让你面对"到底以哪份为准"的无解冲突。建议做法:把 gateway `src/services/knowledge/` 的每个文件和 apps 那份 diff,把独有功能搬过去,然后整个目录删除。

### P3 · Ingestion 路径是 KB 的真拆分点
knowledge-service 最容易被"假装拆完"的地方是:**路由层走了 proxy,但后台 ingestion worker 还留在 gateway 里**。这也是最容易检测的地方 —— 停掉 knowledge-service 容器,观察 gateway 日志 60 秒,如果还有 `embedding` / `upserting to qdrant` / `processing document` 这类关键字,就是没拆干净。

### P4 · Auth 契约早于 route 迁移
每迁一条 route 前,先保证 gateway 对下游注入的 header 集合完整(至少包括 `user_id / tenant_id / tier / type / roles / email`),并且 strip 列表也同时扩展。否则每迁一条 route 都在重复"降级 admin → user"这个 bug。

### P5 · 先装上红绿灯,再动路由
Observability 不是事后加的。拆分开始前,gateway proxy 层必须有:
- `proxy_requests_total{service, status}` metric
- `proxy_breaker_state{service}` gauge
- `proxy_ttfb_ms{service}` histogram
- request_id 从外部 → gateway → 下游服务的端到端贯穿 log

没有这些,你拆完之后根本不知道有没有回归。

---

## 四、红线 — 不可妥协的五条

不是建议,是"踩了就要回滚"。

1. **不允许在 commit 里声明某一阶段 "complete",除非 `scripts/verify.sh` 全绿退出。** 叙事先于验证 = 欺骗。
2. **不允许把 gate 阈值往松改来"通过"验证。** 例如把 `from assistant_service` import 数从 `== 0` 改成 `<= 5`。要么真修掉,要么在 "Known gap" 里明说。
3. **不允许 `|| true` / `2>/dev/null` 包裹验证命令。** 任何吞错行为使验证失效。
4. **不允许在没有合并代码前就删除一方。** KB 那两份必须先 reconcile 再删,否则会悄悄丢功能。
5. **不允许保留 dead code 作为"fallback reference"。** 比如 `assistant.py` 里 `return` 之后的 90 行 unreachable 代码。要么改成显式 feature flag 分支,要么直接删。留着就是 maintainer trap。

---

## 五、验收范式 — 怎么判断"拆完了"

不给你每条 route 的具体命令,只给**判定风格**。你自己写 `scripts/verify-extraction.sh`,必须包含以下类别的断言:

- **静态**:`grep -rE "<forbidden-import-pattern>" src/ | wc -l` 必须为 0
- **构建**:移除某个服务目录后 gateway 镜像仍能构建 + 启动 + `/health` 200
- **运行时**:下游容器 kill 之后,gateway 对应 routes 的 HTTP 码矩阵落在 {502, 503, 504},不出现 500 或超时挂死
- **数据路径**:下游容器 kill 60 秒内,gateway 日志不出现该服务的业务关键字(例如 `embedding`、`qdrant upsert`、`tool_call`、`chat_stream_started`)
- **安全**:从宿主机公网 IP 直打服务端口 → connection refused;从内网带错 secret → 401
- **契约**:一个携带 admin role 的合成 JWT,经端到端后下游日志中看到 `roles=["admin"]`

**格式上**每条断言都长这样:

```bash
# Claim: gateway 不再持有 KnowledgeWorker
N=$(grep -c "KnowledgeWorker(" src/main.py)
test "$N" = "0" || { echo "FAIL: gateway still instantiates KnowledgeWorker ($N hits)"; exit 1; }
```

**你可以自由命名、自由分组、自由选工具**(bash / pytest / tox),唯一要求:
- 每条断言都是二元判断
- 失败即整体失败,不允许部分绿
- 能被用户在自己机器上原样跑出同样结果

---

## 六、Prompt 范式 — 给 Claude Code 自己用

下面是几个可直接复用的 prompt 模板,按任务类型分。写 prompt 时**避免使用模糊动词**("clean up", "improve", "refactor"),用**目标态 + 验收**的格式。

### 范式 A · 单次拆分任务

```
读 plans/TechWhitePaper-Service-Extraction-2026-04-23.md,理解其中的北极星(§二)
和红线(§四)。

本轮目标态:
- <目标态一句话,比如 "knowledge-service 的 ingestion worker 不再在 gateway 进程里跑">

判定完成的唯一标准:
- <把北极星 §二 中相关的几条复制过来,或者写新的二元断言>
- 跑 scripts/verify-extraction.sh 全绿

不允许:
- 修改本文档里的红线
- 声明完成但 verify 未过
- dead code 保留作为 fallback

交付:
- 源码改动
- scripts/verify-extraction.sh 里对应的新断言
- PR 描述里贴 verify 脚本的完整 stdout
```

### 范式 B · 架构决策(需要 ADR)

```
读白皮书 §三(指导原则),尤其是 P<N>。

问题:<具体决策点,例如 "session CRUD 应该归 assistant-service 还是保留在 gateway 作为公共会话层">

你的输出:
- 一份 plans/ADR-XXX.md,不超过 200 行
- 格式:Context / Options / Decision / Consequences / Verification
- Verification 段必须包含未来如何验证这个决策还在被遵守的命令

不要直接动代码。先让我 review ADR。
```

### 范式 C · 回归测试加固

```
读白皮书 §五(验收范式)。

为以下 audit finding 各写一个回归测试:
- H-2 (auth roles 丢失): <link>
- H-3 (smuggle header 不全): <link>
- M-2 (breaker 不记 stream-mid failure): <link>

每个测试:
- 独立可跑 (pytest -k)
- 失败消息清楚指出违反了哪条 finding
- 加到 CI

禁止:
- mock 整个 proxy 层 — 测试要覆盖真实 httpx 行为
- 只测 happy path
```

### 范式 D · 现状核查(任何人都可以发)

```
读白皮书 §二(北极星)。

用当前 HEAD 的实际代码 + 生产实测,对七条北极星逐条给 verdict:
- ✓ 成立 / ✗ 不成立 / ? 证据不足

不允许沿用任何 commit message 或 docstring 的叙事。只看代码、只看 curl 输出、
只看 docker logs。不能访问的部分明确写 "未验证"。

输出 ≤ 300 行 markdown。
```

---

## 七、与之前两份文档的关系

- `plans/Audit-Assistant-Proxy-Current-HEAD-2026-04-23.md` — 针对 `ea5eea7..HEAD` assistant-service 的审计证据。事实底料,不是蓝图。
- `plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md` — 上一份详细设计。**用户反馈过细,Claude Code 的判断空间被压缩**。保留作为"当你卡住不知道具体怎么做"时的 fallback 参考,不作为必须逐条遵守的剧本。
- 本文(TechWhitePaper) — **优先级最高**。如果与详细设计冲突,以本文的原则为准,Claude Code 自行决定实施。

---

## 八、最后一段话

上一轮 `ea5eea7` 的问题不是技术能力不够,是**叙事跑在了事实前面**。commit message 写 "true microservice isolation" 的时候,只有一条路由走了 HTTP boundary,22 条还在进程内 —— 这种落差**不是偶发失误,是可被制度化消除的**:把判定标准写成二元命题、把验证写成脚本、把"完成"定义为脚本全绿。

你有判断力。你决定怎么拆、先拆什么、用什么抽象。但**拆完的判定权不在你**,在 §二 的七条和 §五 的验收脚本。它们绿了,才叫完成。

— *End of paper.*
