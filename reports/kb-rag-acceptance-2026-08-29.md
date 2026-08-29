# 知识库 RAG 升级验收报告（2026-08-29）

## 1. 结论

**最终结论：CONDITIONAL PASS**

`worktree-kb-rag-upgrade` 已合入并复测 `origin/main@47b7a9b9`，当前功能分支
`HEAD=966c9168`。本轮没有发现仍未解决的 P0/P1 正确性、安全性或数据一致性
缺陷；知识库核心 API、真实 PostgreSQL/Qdrant/Redis 链路、付费模型 QA、真实
双嵌入模型回填、内置浏览器 UI 主链路和合并后回归均已通过。分支没有 push，
也没有反向合入根 `main`。

不能给出 PASS，原因是以下强制发布证据尚未完成：

1. T0 的 200–400 条真实/半真实双语黄金集、来源证明和人工复核未完成；正式
   release pointer 不存在。
2. 真实 `text-embedding-v3 -> text-embedding-v4` 影子回填已成功，但 T0 门禁、
   原子 cutover、真实检索回归和 rollback 没有执行，故 H3 场景 4 仍为 BLOCKED。
3. T6 `bm25_v2` 正式租户放行属于用户保留决策；H1 #29 只能显示阻塞状态。
4. T8.3 `kb-tools` 的实现或退役属于用户保留决策。
5. T4 的 50–200 页真实解析评测、T9 的真实语料灰度净收益证据未完成。
6. H1 #4 的目标合同包含带 `Last-Event-ID` 的 SSE；当前实现是正确的条件轮询，
   没有 SSE。H1 #7 的批量部分成功只完成 mock UI 与活栈 API 验证，未在本轮
   内置浏览器中制造部分失败。

因此，本报告确认的是：**实现分支 merge-ready；正式发布仍被上述证据和用户
决策阻塞。**

## 2. 范围与代码盘点

验收合同：

- `docs/plans/rag-upgrade-prd-2026-08.md`
- `docs/plans/rag-upgrade-prd-addendum-dify-2026-08.md`
- `reports/kb-rag-ui-t5/backend-dependency-handoff.md`

开始修改前已确认 Claude 任务停止；之后没有并发写入。功能分支相对最新
`origin/main` 共 312 个路径：188 新增、118 修改、6 删除，最终无未跟踪或
未提交文件。删除项逐一复核如下：

| 删除项 | 复核结果 |
| --- | --- |
| `retrieval_v2.py` | 无运行时引用；旧检索重复实现退役 |
| gateway `knowledge_repository.py` | gateway 直读 KB 表的旧边界退役 |
| gateway `schemas/kb_tools.py`、`schemas/knowledge.py` | schema 权威迁回 Knowledge Service；边界门禁通过 |
| `web/.../SegmentCard.tsx` | 拆分后的空转组件，全仓无引用 |
| `tests/api/test_upload_session_cleanup.py` | 覆盖已迁入知识库上传/清理回归组；相关门禁仍执行 |

检查了 diff、迁移、删除、未跟踪文件、重复实现、生成物和敏感信息；没有发现
凭据或需要提交的构建产物。`git diff --check origin/main...HEAD` 通过。临时脚本、
浏览器附件和数据库备份均未纳入 Git。

## 3. T0–T9 验收矩阵

状态只使用 PASS / FAIL / BLOCKED；“代码已实现”不能替代正式数据或用户放行。

| 主题 | 状态 | 实际证据与缺口 |
| --- | --- | --- |
| T0 评测基础与门禁 | **BLOCKED** | 单元、迁移、真 Qdrant、金夹具数学和回放入口通过；release gate 按设计退出 2：仅 18 条 dev fixture，缺 200–400 条、完整 provenance、人工复核、可验证 source mix 与 `release-pointer.json`。 |
| T1 摄入生命周期与增量 upsert | **PASS** | 状态机、稳定 ID、staging generation、recover/retry、公平认领、reprocess/reembed、禁用/启用/归档闭环均有回归；活栈旧向量持续可读，失败/竞争自动回队。 |
| T2 检索质量 | **BLOCKED** | 中文 jieba lexical shadow 与 12-case 真实 qwen3-rerank bake-off 通过，遥测/阈值/预算/降级/缓存版本已实现；正式默认模型晋升仍受 T0 阻塞。 |
| T3 嵌入蓝绿迁移 | **BLOCKED** | 版本元数据、绑定、持久 202/job、回填/恢复/冲突/取消/授权代码与测试通过；真实 v3→v4 回填成功，但未执行 gate/cutover/rollback。 |
| T4 解析与无损 IR | **BLOCKED** | IR、页块/附件、解析器级联、缓存/并发和 PDF 内存上限有代码与回归；本轮真实 2 页 PDF 主链路通过，50–200 页真实形状与 TEDS/CDM/VLM 指标未完成。 |
| T5 前端优先管理面 | **BLOCKED** | 主管理、段落、评测、遥测、迁移面均已实现并通过 build/mock/内置浏览器；H1 #4 SSE 和 #29 放行缺失，#7 未做内置浏览器部分失败实测。 |
| T6 BM25 v2 生命周期 | **BLOCKED** | 排他锁、持久 lifecycle、并发、回滚、kill switch 和 UI 收据通过测试；正式租户切换/回滚未获用户放行。 |
| T7 可观测性与供给可靠性 | **BLOCKED** | `/metrics`、持久队列状态、锁定镜像、PDF offload、Redis 幂等和配置收敛通过；Helm/真实 Grafana 告警未在本轮环境验证。 |
| T8 gateway 契约与权限 | **BLOCKED** | T8.1/2/4 PASS：scope、ACL 权威、gateway 无直读 KB 表/自定义 KB schema；双账户活栈越权返回 403。T8.3 `kb-tools` 处置待用户决定。 |
| T9 父子检索与结构路由 | **BLOCKED** | 父子/摘要/附件结构代码与回归存在；缺真实语料灰度、质量净胜和默认开启证据。 |

## 4. H1 前端 1–29 项矩阵

| # | 管理操作 | 状态 | 证据 |
| --- | --- | --- | --- |
| 1 | 数据集创建与配置 | **PASS** | 内置浏览器创建真实 v4 数据集，刷新后配置持久。 |
| 2 | 数据集删除 | **PASS** | 活栈 API 清理成功；UI 密码门禁有 mock 回归。验收展示数据集按约保留。 |
| 3 | 文件/URL/文本与批量上传 | **PASS** | 内置浏览器真实 PDF；活栈 API 文本与批量；上传进度可见。 |
| 4 | 阶段进度与时间戳 | **BLOCKED** | 阶段时间戳、刷新和条件轮询通过；修复跨 generation 旧时间戳泄漏；合同要求的 SSE/`Last-Event-ID` 未实现。 |
| 5 | 单文档 reprocess | **PASS** | 内置浏览器 Queued→Completed；API 与状态机回归通过。 |
| 6 | 单文档 reembed | **PASS** | 内置浏览器 Queued→Completed；重复请求 409；旧 generation 持续可读。 |
| 7 | 批量重索引/删除 | **BLOCKED** | 真批量 API 202、>200 与部分成功 mock 通过；本轮未在内置浏览器制造部分失败。 |
| 8 | 失败文档 recover/retry | **PASS** | 持久状态分支、worker 重启和一键重试的集成/mock 回归通过。 |
| 9 | 文档启用/禁用 | **PASS** | 内置浏览器验证 Disabled→Queued→Completed，并核对“重建完成前仍禁用”。 |
| 10 | 文档归档/恢复 | **PASS** | 内置浏览器验证 reason、Archived→Queued→Completed，并刷新确认。 |
| 11 | 类型化文档元数据 | **PASS** | registry、类型校验、批量编辑前后端回归通过。 |
| 12 | 版本历史/比较/恢复 | **PASS** | 原功能保留，Node/mock/后端回归通过。 |
| 13 | chunk 浏览与全字段编辑 | **PASS** | 内置浏览器保存 text、answer、keywords，刷新后持久。 |
| 14 | chunk 启用/禁用 | **PASS** | UI 合同、活栈 API 与 Qdrant 同步回归通过。 |
| 15 | chunk 关键词编辑 | **PASS** | 内置浏览器保存 `AURORA-7291, emergency, 验收`。 |
| 16 | chunk hit_count | **PASS** | 检索后 UI 显示并增长；最终展示值 16。 |
| 17 | 分块预览 | **PASS** | 既有功能保留；前端与 API 回归通过。 |
| 18 | 全模式分块配置 | **PASS** | D6 全字段 schema 往返与前端保存回归通过。 |
| 19 | 检索配置无损保存 | **PASS** | D6/配置 round-trip 测试；内置浏览器预设与配置面可见。 |
| 20 | 检索预设 | **PASS** | 内置浏览器 Balanced/SOTA 实际 A/B 执行。 |
| 21 | hit-testing | **PASS** | `AURORA-7291` 命中 2 段并展示阶段分与引用。 |
| 22 | 评测工作台 | **PASS** | 内置浏览器新增、标注、保存并运行 A/B；两预设最终均 rerank applied。 |
| 23 | 检索结果送黄金集 | **PASS** | UI mock + 活栈持久化 API 验证；没有冒充正式 T0 黄金集。 |
| 24 | QA + SSE + 引用/计时 | **PASS** | 真实 `qwen3.7-plus` 回答、2 引用、16.569s LLM/2.388s retrieval。 |
| 25 | 反馈与理由码 | **PASS** | 内置浏览器点击 Useful；D3 写端与查询读回通过。 |
| 26 | 查询日志/零结果 | **PASS** | 内置浏览器 Query Insights 显示查询、2 hits 和阶段计时；D5 读端通过。 |
| 27 | 嵌入模型蓝绿管理面 | **PASS** | 设置页展示 serving generation、collection、任务与回滚操作；真实 v3→v4 backfill job 成功。发布切换仍归 H3 BLOCKED。 |
| 28 | 集合/索引健康收据 | **PASS** | 内置浏览器展示 collection 与健康收据，未知状态不会伪装健康。 |
| 29 | lexical_v1/bm25_v2 配置 | **BLOCKED** | UI 明确显示 H1 #29 / T6 待用户放行；未悄悄启用。 |

**H1 总结：BLOCKED**（#4、#7、#29）。

## 5. 后端依赖 D1–D8

| 合同 | 状态 | 证据 |
| --- | --- | --- |
| D1 文档列表补列 | **PASS** | lifecycle、阶段时间戳、错误、archived、embedding generation 等列表字段完整；活栈/UI 验证。 |
| D2 `archived_reason` 宽度 | **PASS** | 迁移与 255 字符边界回归通过。 |
| D3 反馈表/端点 | **PASS** | tenant-scoped upsert/read；内置浏览器 Useful 反馈可在查询侧读回。 |
| D4 文档/段落分页 | **PASS** | cursor/limit 与 >200 批量回归通过。 |
| D5 查询遥测读端 | **PASS** | `/queries` 活栈返回；修复 JSON 字符串 `stage_timings` 导致的 500。 |
| D6 配置字段往返 | **PASS** | `ChunkingConfigSchema` 与 retrieval config 全字段 round-trip 测试通过。 |
| D7 黄金用例删除 | **PASS** | tenant-scoped 持久删除端点及 UI 回归通过。 |
| D8 QA/hit-test `trace_id` | **PASS** | 内部关联保留，公开响应不泄露内部 trace；schema 回归通过。 |

## 6. H2/H3 七个场景

| # | 场景 | 状态 | 实际或最高保真证据 |
| --- | --- | --- | --- |
| 1 | 修改单段后 reprocess，仅变化块更新且 ID 稳定 | **PASS** | PostgreSQL/Qdrant 集成断言点集差异；内置浏览器编辑后 reprocess 完成。 |
| 2 | reprocess 并发检索无空窗 | **PASS** | publication fence 与 zero-window 集成测试；活栈操作期间旧结果持续可检索。 |
| 3 | indexing 时 worker 中断续跑且不重解析 | **PASS** | 真 PostgreSQL 持久任务的 crash/reclaim 测试，恢复后 `parser_calls=0`。本轮未杀共享活栈进程。 |
| 4 | 嵌入模型切换、中断续跑、cutover、rollback | **BLOCKED** | 真实 v3→v4 影子回填完成；T0 gate、cutover、检索复验与 rollback 未获放行/未执行。 |
| 5 | 文档禁用→启用恢复一致 | **PASS** | 内置浏览器与 API 验证 DB/Qdrant/缓存状态和最终检索。 |
| 6 | 双文档重嵌 + 批量任务公平、无双认领 | **PASS** | 两租户公平队列/`SKIP LOCKED` 真 PG 测试；活栈 batch contention 自动回队。 |
| 7 | 重嵌失败保留旧向量并可重试 | **PASS** | Qdrant/PG publication 与故障注入测试；活栈 lease contention 不清空旧代并自动回队。 |

**H2：PASS。H3：BLOCKED（场景 4）。**

## 7. 实际执行的门禁与结果

| 命令/操作 | 结果 |
| --- | --- |
| `make harness-check` | PASS；1 个已知 warning：`deploy/runbooks/kb-rag-ui-t5` 的旧 loop-state schema。 |
| 触及 Python 路径的 `ruff check` | PASS，0 error。 |
| `make kb-unit-gate` | PASS：1824 passed，1 skipped（缺专用 backfill PG DSN；迁移真库门禁另行覆盖）。 |
| `POSTGRES_CLIENT_CONTAINER=ai-gateway-pg make kb-migration-gate` | PASS：131 passed。 |
| `pytest tests/scripts` 对应门禁 | PASS：354 passed，2 skipped（专用 KB DSN；本地无 jieba 的分支）。 |
| 真实中文 lexical shadow（含 jieba） | PASS：simple 0.167、bigram 0.833、jieba 0.917；BGE-M3 未配置，明确 skipped。 |
| `make gateway-kb-boundary-gate` + gateway KB 回归 | PASS：114 passed。 |
| 前端 Node 单测 | PASS：94 passed。 |
| `pnpm -C web lint` / i18n / build | PASS；`oversizedRoutes=[]`。Node 24 与项目期望 Node 22 有 warning，但未影响结果。 |
| `pnpm -C web type-check` | exit 0，但确认它是假门禁，不作为证据。 |
| `pnpm exec tsc --noEmit -p tsconfig.app.json --ignoreDeprecations 6.0` | exit 2：97 个仓库存量诊断；与干净 base 差分后本分支新增 0，Knowledge 路径 0。 |
| mock Playwright 全量 | PASS：40 passed（46.9s），含持续 >30s 后出现 503 的轮询场景。 |
| `make kb-golden-gate` | PASS：18 条 dev fixture；不等同正式发布集。 |
| `make kb-image-lock-gate` | PASS：6 passed。 |
| `make kb-release-evidence-gate` | **BLOCKED（预期 exit 2）**：5 个正式证据缺口，见 T0。 |
| 真实 qwen3-rerank bake-off | PASS（开发证据）：12 case，nDCG 0.967911，MRR 1.0；不是 release evidence。 |
| `make migrate`（合并 main 后） | PASS/no-op：锁正常，107 个历史迁移已登记。 |
| `make status && make validate` | PASS：PG/Redis/Qdrant/Knowledge/worker/Gateway/frontend/Agent Runtime 健康；仅本地默认密码 warning。 |
| 活栈 API 回归脚本 | PASS：64 个请求断言；文本/PDF、检索、QA、反馈、遥测、生命周期、批量、迁移、409/404/403、刷新持久性和清理。 |
| 内置浏览器人工回归 | 核心 UI 主链路 PASS；详见 §9。 |

没有把 503 mock、开发 fixture、聊天模型 QA 或影子 backfill 分别冒充真实健康
服务 503、正式黄金集、嵌入模型切换 gate/cutover/rollback。

## 8. Docker、代码版本与迁移状态

在 Docker/E2E 前已阅读 `docs/harness/runtime-and-secrets.md`。没有并行启动冲突
栈；根 checkout 的栈先安全停止，未删除 volume、未 prune。根数据库先备份为
未跟踪文件 `gateway_20260829_064104.sql.gz`（约 80 MiB）。

Compose 标签确认 `config_files`、`working_dir` 均指向本功能 worktree。镜像：

| 服务 | 镜像 ID |
| --- | --- |
| Gateway | `sha256:3f95168a97cb32ed94a3d890f04c8cfaa1b80a195b3f442ccf125a61f9406480` |
| Frontend | `sha256:22844d4f0b1deebfa848ac267c6517d023c1e80d6aaa3109991c94b5f677f418` |
| Knowledge / Worker | `sha256:38bc87f86979dcd7d8509cbdff577e5fd69cf42f6f9119e2bb610b7d3229a6ff` |
| Migrate | `sha256:3a920302ab6341572370c83a09449d48e8fd34641d1befa4a25dafa2b2c8a66c` |

合并 main 后使用 `make hot-update ARGS="--all"` 更新 Python 源码和本地前端构建，
避免无必要的依赖层重建；Knowledge、Gateway、Frontend 的宿主/容器文件 SHA
逐项一致。上述镜像 ID 是构建基底，热更新后的容器文件校验才是“实际运行
HEAD 代码”的证据。

迁移 100–105 在 fresh public layout、从 main schema 升级、已完成 per-service
split 的旧库中执行；canonical CLI 与 shell 都设置 Knowledge-first
`search_path`。legacy `run_migration.py` 对这些迁移 fail-closed。重复启动、账本、
约束、并发 runner 和数据保留均通过。迁移 100–110 共 11 条已在 public ledger
登记。

100–105 新增的 8 张表均位于 `knowledge` schema，owner 为执行 canonical runner
的数据库角色（本地为 `postgres`），不存在 public/其他 schema 重复表：

- `dataset_collection_bindings`
- `document_pipeline_executions`
- `embedding_migration_progress`
- `embedding_migrations`
- `embedding_vector_cache`
- `kb_bm25_v2_lifecycle`
- `kb_eval_golden`
- `kb_eval_golden_release`

迁移 101 会改写状态/hash、替换唯一约束并扫描/锁表。N-1 Knowledge binary 不能
作为安全回滚路径；本升级明确标记为 **restore-required**。真 PostgreSQL
`pg_dump -> migrate -> pg_restore` 验收通过，不能把“部署旧镜像”写成已验证回滚。

## 9. 内置浏览器实机验收

使用仓库规定的专用 E2E 账号，在 Codex 内置浏览器完成登录；未修改管理员密码，
未打印或保存凭据。创建并保留数据集：

- 名称：`iab-rag-acceptance-1788003121114`
- ID：`kb_35d11ebc320f`
- 当前页面：`http://localhost:8081/knowledge/kb_35d11ebc320f?tab=documents`
- 嵌入：真实 `text-embedding-v4`

实机结果：PDF 上传→摄入完成（2 segments）；`AURORA-7291` 检索命中 2 段；
付费 `qwen3.7-plus` QA 返回正确答案和 2 个引用；反馈、Query Insights、
hit_count、段落全字段编辑、文档禁用/启用、归档/恢复、reembed、reprocess 均
完成并在刷新后保持。评测工作台完成用例新增、标注、保存和 Balanced/SOTA
A/B；一次 SOTA 瞬时 fallback 被 UI 如实显示，随后的重试两侧均为
`dashscope/qwen3-rerank` applied、IR 指标通过。设置页展示迁移任务、集合健康和
H1 #29/T6 阻塞状态。

修复阶段时间戳后，再次在内置浏览器触发 reembed：新 generation 不再显示上一
代的 “Splitting 2m51s”，最终 Completed。数据集故意保留，便于用户直接查看。

## 10. 本轮发现并修复的问题

| 严重度 | 问题 | 修复与回归 | 提交 |
| --- | --- | --- | --- |
| P1 | Qdrant 非瞬态 404 被重试，删除可能卡住 >120s | 404 分类为终止错误；14 个定向测试；活栈清理约 0.64s | `4e332b24` |
| P1 | Compose 将幂等后端降级为 memory | 目标栈强制 Redis，验证跨副本 fail-closed | `4e332b24` |
| P1 | `/queries` 遇到字符串化 `stage_timings` 返回 500 | 行解码统一 JSON 反序列化并补回归 | `4e332b24` |
| P1 | API 角色的 enable/unarchive 错判“无 worker” | 接入 durable enqueue proxy；路由与服务测试 | `4e332b24` |
| P1 | batch reembed 与蓝绿 publication lease 竞争会把文档标成普通失败 | 明确传播 lease contention，worker 原子回队；旧向量不清空 | `4e332b24` |
| P1 | 迁移 action 是同步 HTTP，可能超过 Gateway 30s | backfill/verify/gate 改 durable `202 + job_id`，覆盖断连、重试、并发 | `4e332b24` |
| P1 | 100–105 未限定对象名且 legacy runner 继承默认 search_path | canonical Knowledge-first、全限定对象、legacy fail-closed；三种 layout 真库测试 | `4e332b24` |
| P1 | UI 暗示“清理后重试”，与旧代持续可读合同冲突 | 文案和行为改为原子完整重试；失败/取消恢复旧代 | `4e332b24` |
| P1 | 新 generation 继承旧阶段时间戳，UI 显示虚假长耗时 | 仅新 generation 清理阶段时间戳；same-generation recover 保留 | `966c9168` |
| P1 | >30s mock 在响应落地前断言，偶发失败 | 等待注入 503 后再断言；产品持续轮询语义不变 | `966c9168` |

功能提交与验收提交保持可审查分组：

- `62587dc9 feat(kb): implement RAG upgrade backend and migrations`
- `df6536a4 feat(web): complete knowledge upgrade workflows`
- `4e332b24 fix(kb): close acceptance and release-gate gaps`
- `f8c91b05 Merge remote-tracking branch 'origin/main' into worktree-kb-rag-upgrade`
- `966c9168 fix(kb): close live acceptance findings`

## 11. 未验证项与用户待决策项

交给后续 Luna 或发布负责人时，不应再花时间重复已通过的 1824 单测和主 UI
链路；优先处理/决定：

1. 建立 200–400 条正式黄金集、provenance、人工复核和 release pointer，重跑
   `make kb-release-evidence-gate`。
2. 在允许的测试租户执行完整 v3→v4 gate→cutover→并发检索→rollback；记录
   断连、续跑和回滚收据。
3. 用户决定 T6 是否允许 `bm25_v2` 正式切换/回滚。
4. 用户决定 T8.3：实现 KS `kb-tools` 真检索面，或显式退役。
5. 决定是否把 H1 #4 从条件轮询升级为 SSE/`Last-Event-ID`；若合同不改，此项
   是发布阻塞。
6. 用内置浏览器制造一次批量部分失败，核对逐项错误、刷新持久性和重试。
7. 完成 T4 真实长 PDF 指标和 T9 真实语料灰度。

## 12. 与 main 的集成方式

本分支已正常 merge 最新 `origin/main@47b7a9b9`，没有 Git 冲突，合并后完整门禁
和活栈回归通过；没有改写历史、force push、push 或反向合入 main。

根 main checkout 的未提交架构文档/harness 工作完整保留且未修改。它与本分支
实际重叠的文件只有：

- `docs/harness/commands.md`
- `harness.yml`

建议集成顺序：先在根 main 单独提交架构文档/harness 改动；然后以普通 merge
合入 `worktree-kb-rag-upgrade`，仅对上述两个文件人工保留双方命令合同；最后重跑
`make harness-check`、KB/migration/gateway/frontend 门禁与活栈 smoke。不要在根
dirty checkout 上直接覆盖、stash 或强行合并。
