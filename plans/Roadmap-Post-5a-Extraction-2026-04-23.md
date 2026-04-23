# Roadmap — Post-5a Extraction

**Starting point**: Phase 5a complete(基础设施铺好,0 条路由迁移)
**Finishing line**: 白皮书 §二 北极星七条全绿
**North star drift budget**: 每个 Phase 至少让 1 条北极星从 ✗ → ✓;做不到的不合。

---

## 0. 路线总览

```
                Assistant track                      Knowledge track
                ────────────────                      ────────────────
Phase 5a ✓      基础设施(共享 proxy、HMAC、端口收紧、dead code 清理)
                │
                ├──────────────────────┬──────────────────────┐
                ▼                      ▼                      ▼
Phase 5b        搬只读/轻量 CRUD        Phase K5b             合并两份 fork
                (models/config/tools/   (knowledge-service
                 policies/runs)          成唯一权威源)
                │                      │
                ▼                      ▼
Phase 5c        搬 stateful            Phase K5c             ingestion worker
                (sessions/artifacts/                          彻底移出 gateway
                 image)
                │                      │
                └──────────────────────┴──────────────────────┐
                                                              ▼
Phase 5d/K5d    清理 gateway 内残留(删 import / 删 fallback / 瘦 Dockerfile)
                │
                ▼
Phase 5e        生产切换 + 观测 + 端到端验证
```

两条轨道可并行,**但 5d 必须在 5c 和 K5c 都完成后才开始** —— 在仍有 in-process fallback 时就删 import,只会把 rollback 路径炸掉。

---

## 1. 北极星推进表

| Phase | 让哪几条变绿 | 允许保留的 transitional |
|---|---|---|
| 5a ✓ | 本轮不改变北极星 | — |
| 5b | #6 网络边界绿(prod 部署 secret + 关 8093 publish)+ #7 Auth 契约绿(e2e contract test 覆盖 roles / type) | gateway 仍有 in-process 业务栈做 fallback |
| K5b | #2 KB 源单一权威(gateway 不再 fork 代码,只留 thin client) | gateway 进程内仍有 KnowledgeWorker(留到 K5c 拆) |
| 5c | #3 启动独立(kill assistant-service,gateway 起得来)+ #5 数据路径单一(sessions 表只有 as 写)| in-process fallback 改 feature flag 控制,默认关闭 |
| K5c | #5 KB 数据路径单一(`documents` 表只有 knowledge-service 写)+ confluence sync 通过 API 调用 knowledge-service | — |
| 5d/K5d | #1 编译时解耦(0 个 `from assistant_service/knowledge_service` import)+ #4 运行时不共栈 | — |
| 5e | 全部 7 条绿 + 生产实测验证 | — |

---

## 2. 各 Phase 目标 + Claude Code prompt 模板

每段遵循:**Goal 一句 → Done when(北极星)→ 主要杠杆(不超过 3 个)→ Prompt 模板**。
不给具体文件列表、不给 PR 结构、不给时间估算 —— Claude Code 自己决定。

---

### Phase 5b · Assistant 只读/轻量 CRUD 搬迁

**Goal**: 把 models / datasets / config / tools / policies / runs / approvals / chat(非 stream) 这类**幂等或简单写**的 route 搬到 assistant-service,gateway 侧改为 proxy,保留 in-process handler 作 feature-flag 兜底。

**Done when**:
- 北极星 #6(网络边界)→ ✓:生产 EC2 `curl http://<public>:8093` connection refused;带错 secret → 401
- 北极星 #7(Auth 契约)→ ✓:新增 `tests/contract/test_auth_e2e.py`,覆盖 roles=["admin"] / user_type / tier 穿透验证,CI 绿
- 被搬的每条 route 的 gateway 端代码行数 ≤ 15 行(只剩 authz + proxy.forward)

**杠杆**:
1. **先写 e2e auth contract test,再搬 route**。没这套 test 就搬 route,等于把 Phase 5a 的 inject/parse 改动带着 bug 一起搬过去。
2. **Feature flag per route**(比如 `ASSISTANT_ROUTE_MODELS_PROXIED=true`)。每迁一条带 flag,flag 关 = 走老 in-process 路径。flag 默认值在本 Phase 可以先关、灰度 1 天再打开。
3. **MCPManager / ToolRegistry 初始化同步搬到 assistant-service main**,gateway 侧 `app.state.mcp_manager = None`。但是 **in-process fallback handler 这一批还不删**,留到 5d。

**Prompt 给 Claude Code**:
```
读 plans/TechWhitePaper-Service-Extraction-2026-04-23.md §二(北极星)和 §四(红线)。
读 plans/Roadmap-Post-5a-Extraction-2026-04-23.md Phase 5b 段。

本轮目标:让北极星 #6 和 #7 变绿。

开工前先做两件不写业务代码的准备:
1. 写 tests/contract/test_auth_e2e.py,覆盖:
   - admin JWT → gateway → assistant-service,下游看到 roles=["admin"]
   - 用户带 X-User-Type: admin 试图 smuggle → gateway strip 后下游看到正常 "user"
   - gateway 注入的 7 个 identity header 都正确透传
   这个 test 必须真跑 httpx 到 fastapi TestClient,不允许全 mock。
2. 生产 .env 注入 GATEWAY_ASSISTANT_SHARED_SECRET,docker-compose 拉 8093 publish,
   实测 curl http://<public-ip>:8093 connection refused(把输出贴到 PR)。

这两件做完、验证全绿后,再开始搬 route。每搬一条:
  - gateway 侧新实现 ≤ 15 行(authz + proxy.forward)
  - 老 in-process handler 加 feature flag,不删
  - 对应 assistant-service 的 route 必须存在(不许建空 stub 假装)
  - contract test 覆盖新旧两条路径等价

搬哪几条由你决定,但至少覆盖:models / datasets / config / tools / policies / runs。

完成后 acceptance-5b.md 里重新对七条北极星出 verdict,证明 #6 #7 变绿。
不允许声明 "extracted" / "microservice complete"。本 Phase 的正确叙事是
"routes proxied with in-process fallback intact"。
```

---

### Phase K5b · KB 两份 fork 合并

**Goal**: 把 `src/services/knowledge/`(gateway 内)和 `apps/knowledge-service/src/knowledge_service/services/knowledge/` 合并到**后者为唯一源**,gateway 侧变成调用 knowledge-service API 的 thin client。

**Done when**:
- 北极星 #2 KB 部分 → ✓:knowledge 业务逻辑在仓库里只存在一份(`apps/knowledge-service/`)
- `gateway src/services/knowledge/` 只剩 thin client 相关文件(推荐保留 `kb_proxy_client.py` 作为 API 客户端,其他全删)
- gateway 内所有 `from ...services.knowledge.*` import 要么改走 thin client,要么移到 knowledge-service

**杠杆**:
1. **逐文件 diff + merge**。两份已经 drift ~200 行,不能 `rm -rf` 一方就完事。每个 file 分三类:双方相同(随便留一份)、一方独有(搬过去)、两份都改过(人工合并并写明保留哪一方)。
2. **先合并,后改调用方**。把权威源建好,然后 gateway 里的调用者一个一个切换。
3. **Confluence sync 仍保留在 gateway 本 Phase 不动** —— 那部分依赖 in-process KnowledgeWorker,K5c 再处理。

**Prompt 给 Claude Code**:
```
读白皮书 §二 北极星 #2,和 Roadmap Phase K5b。

本轮目标:让 KB 知识库业务逻辑在仓库里只存在一份,位于 apps/knowledge-service/。

步骤(你有判断力,顺序由你定):
1. 把两份 services/knowledge 做 diff 并产出一份 merge report 贴到
   plans/kb-fork-merge-report.md,列出每个文件的状态:
     - identical / gateway-only / kb-service-only / diverged
   diverged 的必须说明保留哪一方、为什么。
2. 按 report 合并到 apps/knowledge-service/ 为唯一源。
3. gateway 内所有 from ...services.knowledge 的 import 要么切走 thin client(通过
   proxy 调 knowledge-service API),要么暂时保留但标记 TODO(K5c)。
4. 删除 gateway 内已完全迁出的文件。确保 src/services/knowledge/ 最终只剩
   kb_proxy_client.py + __init__.py + (confluence/ 子目录,K5c 再拆)。

Done criteria:
- grep -rE "from \\.\\.\\.services\\.knowledge\\.(knowledge_service|worker|retrieval|vector_store|embedding)" src/ 返回 0
  (confluence. 暂时豁免)
- 合并后的 knowledge-service 容器单跑一轮 ingestion + retrieval 不出错
- acceptance-K5b.md 里更新北极星 #2 的 verdict

禁止:
- rm -rf src/services/knowledge/ 不做 diff 就全删
- 两份代码都保留,说"以后再合"
- 假装 diverged 文件 identical
```

---

### Phase 5c · Assistant Stateful 搬迁

**Goal**: sessions / artifacts / generate-image / image-task 搬到 assistant-service;SessionManager 物理归属 assistant-service;gateway 不再直连 session/artifact 相关表。

**Done when**:
- 北极星 #3(启动独立)Assistant 部分 → ✓:生产 `docker stop assistant-service`,gateway `/health` 仍 200,受影响的 assistant routes 返回 502/503,其他功能(KB / auth / files)正常
- 北极星 #5(数据路径单一)Assistant 部分 → ✓:`assistant_sessions` / `assistant_artifacts` 表只有 assistant-service 写

**杠杆**:
1. **SessionManager 物理搬迁要一次完成**,不允许双写。gateway 不再有直接的 `session_manager.create/get/delete` 调用。
2. **Session ownership check 走 API**(gateway 调 `GET /api/v1/assistant/sessions/{id}/owner`),不再直连 DB。
3. **artifact download 保持 streaming**,走共享 ServiceProxy 的 stream 分支,生产抽样验证 TTFB 不劣化。

**Prompt 模板**(同 5b 风格,此处略,用北极星 #3 #5 作 done criteria 替换)。

---

### Phase K5c · KB Ingestion 完全解耦

**Goal**: gateway 进程里不再跑 KnowledgeWorker / VisionPDFProcessor / HierarchicalIndexer / VLMOCRService / ConfluenceScheduler。所有 ingestion 通过 API 触发 knowledge-service。

**Done when**:
- 北极星 #4 KB → ✓:gateway `app.state.knowledge_service / knowledge_worker` 为 None 或不存在
- 北极星 #5 KB → ✓:`docker stop knowledge-service`,gateway 日志 60 秒内不再出现 `embedding` / `upserting to qdrant` / `processing document`
- Confluence sync 通过 REST API 提交 task 给 knowledge-service,不再 in-process 调 KnowledgeWorker

**杠杆**:
1. **ConfluenceScheduler 的 executor 改成 HTTP client**,不再持有 KnowledgeWorker 引用。
2. **所有 `app.state.knowledge_service.xxx()` 调用改走 proxy**。
3. **Ingestion 任务队列的所有权明确属于 knowledge-service**,gateway 只提交任务不消费。

**Prompt 模板**(同上风格)。

---

### Phase 5d / K5d · 清理编译时残留

**Goal**: 删掉 gateway 里所有 `from assistant_service / from knowledge_service` import、删 Dockerfile 的 `COPY apps/...`、删 in-process fallback、gateway pyproject.toml 移除两个服务的依赖。

**Done when**:
- 北极星 #1 → ✓:`grep -rE "from (assistant_service|knowledge_service)" src/ packages/` 为 0
- 北极星 #2 完整 → ✓:gateway 镜像 `docker run ai-gateway:latest python -c "import assistant_service"` → ModuleNotFoundError,同 knowledge_service
- 北极星 #4 完整 → ✓:gateway 启动日志不再出现两个服务的业务对象初始化
- gateway Docker 镜像 size 相比 Phase 5a 前 ≥ 下降 30%

**杠杆**:
1. **Feature flag 默认切到 proxied,观察 3-7 天再开始删**。确认生产无回滚需求再删。
2. **一次删一个 import 族,用 grep 把 0 守住**。每删一个 commit,CI grep 是否仍为 0。
3. **Dockerfile 瘦身最后做**,先确认代码层所有 import 已经走。

**Prompt 模板**:
```
读 Roadmap Phase 5d + K5d。

本轮目标:让北极星 #1 (编译时解耦) 从 ? → ✓。

先确认前置:
- Phase 5b/5c/K5b/K5c 的 acceptance 文档 verdict 里北极星 #6/#7/#3/#5 都已 ✓
- 生产已切到 proxy 路径至少 3 天,没有回滚记录

否则拒绝执行本 Phase,回去先把依赖补完。

执行顺序由你定,但每个 commit 之后:
  grep -rE "from (assistant_service|knowledge_service)" src/ packages/ | wc -l
必须是单调不增的。

最后一步:Dockerfile 里删除 COPY apps/assistant-service,重新 build,
docker run python -c "import assistant_service" 必须 ModuleNotFoundError。
把这个输出作为 acceptance-5d.md 的关键证据。

禁止:
- 为了让 grep 变 0 把 import 改成 importlib.import_module('assistant_service') 这种绕过
- 保留 try: from assistant_service import X except: X = None 这种"看起来 0 import"的写法
```

---

### Phase 5e · 生产切换 + 端到端验证

**Goal**: 新架构在生产跑稳,北极星七条在实际流量下验证绿色。

**Done when**:
- 北极星全 7 条 ✓,且每条 verdict 的证据都是**生产实测**(不是本地 docker-compose)
- chat/stream TTFT p95 不劣化(baseline 762ms)
- `docker stop assistant-service` + `docker stop knowledge-service`(分别做)两次 chaos test 通过
- Prometheus metrics 覆盖 proxy_requests_total / breaker_state / ttfb / auth_failures
- 端到端 admin role contract test 在生产合成 JWT 下通过

**Prompt 模板**:
```
读白皮书 §二 七条北极星。

本轮是生产验证 Phase,不动架构代码。任务:

1. 出一份 plans/production-north-star-verdict-<date>.md,对七条北极星逐条:
   - 证据必须来自生产实测(prod URL + curl 输出 / 容器日志时间戳)
   - 不允许引用本地 docker-compose、CI 输出、或任何历史文档
2. 执行两次 chaos test:
   - ssh prod && docker stop assistant-service; 测 30 秒 + 重启;记录恢复时间
   - ssh prod && docker stop knowledge-service; 测 30 秒 + 重启;记录恢复时间
   gateway 日志不允许出现 5xx 以外的崩溃;受影响 route 必须统一 502/503
3. 部署 Prometheus scrape + Grafana dashboard + alert rule。
4. 生产合成 admin JWT 跑一遍 chat/stream,从 gateway 日志 grep 到 assistant-service 日志,
   断言 roles=["admin"] 贯穿,下游 tool_registry 授权通过 role:admin 工具。

成功标准:七条北极星全绿,且有生产证据。
此时才允许在对外文档里使用 "microservice extraction complete" 这种叙事。
```

---

## 3. 依赖与门禁

```
5b ────┬─── 5c ─────┐
       │            ├── 5d ── 5e
K5b ──── K5c ───────┘
```

门禁规则:
- 5c 合并前,5b acceptance 的北极星 #6 #7 必须是 ✓
- K5c 合并前,K5b acceptance 的北极星 #2 (KB) 必须是 ✓
- 5d 合并前,5c 和 K5c 都必须 ✓,且**生产切换到 proxy 至少 3 天**
- 5e 合并前,5d 的编译时解耦 grep 验证必须 ✓

任何 Phase 的 acceptance 文档必须同时满足:
1. 本 Phase 该变绿的北极星确实变绿(附证据)
2. 已经绿的北极星**没有退化**(每次 verdict 要完整重列七条)
3. verify 脚本全绿退出,stdout 贴 PR

---

## 4. 主要风险

| 风险 | 触发点 | 缓解 |
|---|---|---|
| 搬 route 时假装迁完(route 在 as 侧是空 stub) | 5b/5c | contract test 要求功能等价,不是存在即通过 |
| KB fork 合并时静默丢功能 | K5b | 强制 merge report,diverged 文件必须明说保留哪一方 |
| feature flag 默认值搞错,prod 直接切 proxy 同时老代码还没验证 | 5b 结束 → 5d 开始 | flag 默认值变更必须单独 commit,与删除 import 分两个 PR |
| `app.state.knowledge_worker` 两头同时跑导致 double embedding | K5c 前 | K5b 期间不动 worker;K5c 开始前先把 gateway 的 worker 关掉(只留 as 的),观察 24 小时 |
| Phase 5d 太早(生产还在用 in-process fallback)导致无法回滚 | 5d | 5d prompt 里硬编码"生产切 proxy ≥ 3 天"门禁 |
| 声明 "true extraction" 但北极星没全绿 | 任何 Phase | 白皮书 §四 红线;Phase 5e 前任何 PR 里出现该措辞 = reject |

---

## 5. 不做什么(显式 out-of-scope)

- **不拆 postgres 为两个 cluster**:共享一个实例、不同 table 命名空间足够。
- **不引入 Kafka / RabbitMQ / gRPC**:当前 HTTP + 共享 Redis 队列够用。
- **不上 service mesh / mTLS**:HMAC 足以覆盖当前内网威胁模型。
- **不改前端契约**:gateway 对外 URL / response shape / SSE event name 在整个 5b-5e 期间必须 byte-identical。
- **不并发启动 5b 和 K5b 的搬迁**(基建共享可以并行,业务迁移不行)—— 两头同时动会让 rollback 窗口消失。

---

## 6. 判断本 Roadmap 是否还有效的信号

每两周 review 一次。以下信号任一出现,停下来重做 roadmap:

- 某个 Phase 已经开始但超过计划 2 倍时间仍未过 done 标准
- 北极星出现倒退(某条从 ✓ 回到 ✗)
- 生产出现 5b/5c 相关故障 P1 以上
- 前端发现 gateway 对外契约变了

否则按顺序推进。

---

*End of roadmap.*
