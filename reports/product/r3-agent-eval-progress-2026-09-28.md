# R3 Agent 与 Eval 安全增量执行记录（CANDIDATE_80）

基线 `main@8b3a877a`，工作分支 `codex/r3-agent-eval-20260928`。用户在收口阶段授权提交并本地合入 `main`，未授权推送。范围、并行文件归属和状态以 `deploy/runbooks/r3-agent-eval-2026-09-28/` 为准。此记录区分代码/自动化已通过与 J14～J18 尚未完成的真实验收；**不声明完整 R3 已完成**。

## R2 遗留项分类（本轮只核对一次）

| 项目 | 分类 | R3 处理 |
| --- | --- | --- |
| J09～J13/J30 的最终内置浏览器矩阵 | R100_REQUIRED：开始时 Mac 锁定；最终 IAB 可访问但旧登录态过期 | 本轮最终打开 `/agents` 仍跳到 `/login`；不把独立 Playwright 浏览器充作 Codex IAB 已验 |
| 双租户双角色撤权/分享 | R100_REQUIRED：仅一个已确认既有 E2E 身份 | 保留权限自动化，真实矩阵待既有账号前提；不创建新账号 |
| 已配置连接器的更新/删除/失效凭据恢复 | R100_REQUIRED：当前 Runtime 未配置对应连接器 | 只记录现有 `not_configured` 真实状态，待连接器前提到位 |
| 批量 POST 已成功但响应和 operation ID 完全丢失 | FOLLOW_UP：服务端结果未知，现有 UI 要求人工对账 | 不自动重发；不把人工核对说成确定未执行 |

上述是 R2 未验/后续事项，不是 R3 完成状态。R3 使用已授权的合成知识和现有 Eval 样本库切片；若发现会实际阻断 J14～J18 的 R2 合同缺陷，只修该前提并单列证据。

## R3 初始需求—现状—缺口—验收

| 需求 | 现有入口 | 待判定及验收 |
| --- | --- | --- |
| AG-01～04 | Agent 目录、草稿、预览、资源解析已有实现 | J14 固定草稿 r1/r2、实际模型/工具/KB/记忆权限与失败/冲突回读 |
| AG-05～08 | Runtime 预览、发布页、版本与回滚 API 已有 | J14/J15 审批/取消/刷新、受众、发布固定版本、旧 run 与新 run 归属 |
| AG-09/EV-07 | Trace 与反馈页面/API 已有 | J16 差评/失败一跳到授权 Trace，待审核用例不重复导入 |
| EV-01/02/08 | R2 已把 KB 失败写入权威 pending 修订 | J16 固定数据集/候选快照，真实运行与 Trace 重评分分开，KB 链路可回溯 |
| EV-03～06 | Eval 任务、评分、比较与取消已有实现 | J17/J18 故障/未评分分母、可核对效果、A/B 逐例与不可归因提示、选定失败项安全重试 |

当前表仅指调查入口，不将页面/API 存在记为验收通过。自动化、真实服务、故障注入和内置浏览器结果将分层追加。

## 本轮实现与复核

| 范围 | 修正的用户事实 | 当前证据层 |
| --- | --- | --- |
| AG-03/05 | 预览固定草稿修订；保存 r2 不清除 r1 运行。刷新先核验 Agent/用户/preview 会话 pin，再按同 session/run/thread 恢复状态、审批、终态消息和授权产物；未确认终态时禁止新建、清除或切换预览。产物只显示已保存且可下载项，并通过鉴权 Blob 下载 | 自动化 + Docker 单次真实预览；r1 运行中保存 r2、审批重启仍待真实故障场景 |
| AG-04/06/07/08 | 发布评测冻结受众与公开截止时间，过期后运行和回滚均拒绝。tenant/API Token/Embed/public 使用对应实际读者身份预检；匿名身份经签名传到 Knowledge Service，仅公开 Dataset 可读，避免公开 Agent 暴露租户可见资料 | 自动化 + 本地 Docker 私有 Dataset 匿名拒绝；公开 Dataset/双角色真实矩阵未配置 |
| EV-01/02/08 | 用例 PATCH 验证合并后的 trajectory/assertions；重新评分冻结版本和哈希，完整样本仅在私有 outbox，公开 run/experiment 输出连旧快照也剥离样本正文 | 自动化，未跑 KB 撤权后的真实 Eval 执行 |
| EV-03/04/05 | 执行故障、未评分、质量分和发布 gate 分开；裁判/候选故障不算质量 0；部分 live run 失败；A/B 对比标明相同失败、未评分与证据不足 | 自动化，真实 provider 故障/裁判故障未注入 |
| EV-06 | 只对用户明确选定且可证明无工具副作用的失败样本新建单次 attempt；未知执行、工具轨迹、KB 来源不明或撤权均拒绝。幂等键跨刷新保存，同一请求重放返回同一 run，不自动执行 | 自动化，真实重复 POST/进程崩溃未演练 |

独立只读 reviewer 首轮指出 Eval PATCH 校验遗漏、重新评分快照越权、直接评测缺失数据集返回 500、发布按 owner 预检、匿名 KB 越权、预览刷新后的重复执行与产物下载不鉴权、重试未知副作用及无幂等。主代理协调最小修复后，reviewer 复核最终 A/B/C 差异，**没有确认的合入硬阻塞**。该结论是静态 review，不代替下述实测。

## 已运行的自动化与本地 Docker

| 层 | 实际命令/动作 | 结果 |
| --- | --- | --- |
| 隔离 PostgreSQL authority | `AGENT_PUBLICATION_EXPIRY_DB_LIVE=1 uv run --all-packages --extra test pytest -q --no-cov tests/database/test_agent_publication_expiry_migration_live.py` | 1 通过；冻结 baseline→epoch 7 的结构/ACL/扩展/参考指纹匹配 |
| 发布原子性 | `uv run --all-packages --extra test pytest -q --no-cov tests/database/test_agent_publication_atomicity.py` | 17 通过；隔离测试库经 authority 安装 007 |
| Agent Studio 聚合 | `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost make verify-agent-studio` | **40/40 门禁通过、source_stable=true**；含 Studio 27/27、既有路由 51/51 浏览器模拟用例，证据在 `reports/agent-studio/agent-studio-regression-v1-result.json` |
| Eval/Agent | `make verify-eval-dev`；`make agent-eval-core-gate`；`make eval-e1-gate` | 全部退出 0；Agent core 两组为 18/18 与 99/99，E1 204/204 |
| Knowledge/边界/API | `make kb-unit-gate`；`make architecture-boundary-gate`；`make verify-openapi-contract`；`make harness-check` | KB 2022 通过、1 个需独立 DSN 的用例跳过；架构 0 违规；OpenAPI 兼容、Harness 通过 |
| Web/聚焦 | Web TypeScript、lint、build、i18n；A/B/C 聚焦 Python/Node 回归 | 全部通过；各流记录 A 63、B 155、C 7 Python + 11 Node，通过数存在门禁重叠，不能相加充作独立覆盖 |
| Docker 部署 | Compose owner label 核对；`make doctor`、`make migrate`、`make hot-update ARGS=--all`、`make migrate-status`、`make status` | owner 指向本仓库；epoch 7 已应用；Gateway/Knowledge/Web 源码或入口文件哈希与容器相同；所有服务健康；doctor 仅提示 Docker 3 GiB 内存低于建议 4 GiB |
| 真实 Docker 浏览器 | 现有 E2E 账号 + `playwright.live.config.ts`，`E2E_EXISTING_ACCOUNT_ONLY=1`，仅运行 `agent-studio-live.spec.ts` 和 `agent-publish-live.spec.ts` | **2 通过、1 因未启用回滚 feature flag 而跳过**。真实创建/保存/刷新/预览并收到非 stub 回复，内部发布/回滚/资源阻断通过；临时 Agent 按测试清理。原始非敏感结果见 [`r3-live-e2e-2026-09-28.md`](r3-live-e2e-2026-09-28.md)，截图在 `reports/agent-studio/as-05/` 和 `as-06-screenshots/` |
| 真实 Knowledge 签名链路 | 在最终 Gateway 容器调用 Resolver，使用既有私有 Dataset 和匿名身份 | `anonymous_private_dataset=denied`；无租户可见/公开 Dataset fixture，因此该两类未实测 |
| Codex 内置浏览器 | 最终访问 `http://127.0.0.1:8081/agents` | 跳转 `/login`，旧登录态已过期，**认证后的 J14～J18 IAB 验收未执行** |

聚合门禁前两轮曾失败：第一轮并发写文件造成源不稳定和前端 import 瞬态；第二轮暴露模拟 API 未跟进预览历史读取、隔离发布测试库未应用 007、以及本机代理对 localhost 临时 nginx 返回 502。分别修复夹具/迁移流程并设置 `NO_PROXY` 后，第三轮 40/40 通过。前两轮不计通过。

## J14～J18 收口判定

| 场景 | 当前判定 | 剩余 R100_REQUIRED |
| --- | --- | --- |
| J14 草稿 r1/r2 与预览 | CANDIDATE_80：真实单次草稿/保存/刷新/预览通过，r1 运行中保存 r2 的自动化通过 | 真实长 run 中修改草稿、审批/取消/产物回看，Codex IAB 登录恢复 |
| J15 内部发布与回滚 | CANDIDATE_80：真实内部发布、阻断、回滚通过 | 双角色权限、公开/Token/Embed 实际受众和失效期限的真实矩阵 |
| J16 Trace→样本→真实候选 | 自动化通过关键合同 | 合成知识五类用例的真实 A/B 执行与来源撤权回查 |
| J17 provider/裁判失败/取消 | 自动化和故障分类通过 | 真实 provider/裁判受控故障、进程恢复与选定样本重试对账 |
| J18 固定 A/B 比较 | 自动化证明不可归因/同失败/未评分分类 | 真实固定候选同一数据集逐例比较和发布决策回读 |

本地 `main` 合入的是可复核安全增量；R3 **尚未达到 J14～J18 全部真实验收**，完整 R3 不标完成。未推送远端；保留开始时已有的 `reports/r1-boundaries/*.json` 未跟踪文件。

## 2026-09-28 续验修正

上述“旧登录态过期”只是页面现象，不是充分的阻塞判断。同一已有 E2E 身份在本地 Docker Playwright 已成功登录。主代理随后在 Codex IAB 尝试正常登录：该浏览器使用独立虚拟剪贴板，系统剪贴板粘贴无数据；打开本机凭据文件的 `file:` URL 被浏览器安全策略明确拒绝，并明确禁止通过间接路径绕过。已停止绕过尝试并异步请求用户在 IAB 手动登录；其余 API/真实服务验收继续进行。此项仅是 **IAB 交互输入依赖**，不能阻断可独立执行的 J14～J18 后端/Playwright 工作。未输出或保存账号凭据。

继续调查确认另一项 **R3 范围内的代码缺口**：现有 Eval `live_candidate` 经 V2 Thread/Turn 真实执行，但启动的是内建 Assistant，既没有 Studio Agent ID，也没有固定草稿/版本身份。因此此前自动化的 A/B 分类不能声称已经验证“固定 Agent A/B”；也不能把 `rescore_trace` 当新执行。后续使用现有预发布不可变 Version 作为 typed Eval target，由 Gateway 服务端解析版本与当前资源授权，保持原 Runtime/Worker 执行和 durable handle；五类样本真实 A/B 通过后再核发布决策身份。该工作仍在进行，未改变本报告上方已通过的安全增量证据。

## 2026-09-28 固定 Version 安全增量收口

上段所述 typed target 已实际实现：Eval 的 `live_candidate` 接收服务器核验的 `agent_id`/不可变 `agent_version_id`，冻结模型、KB、spec/runtime 指纹；worker 使用原创建者授权，V2 Thread/Turn 核对同一 pin 后执行。Web 可选定和回读版本。内部 linked publish 需显式选择已完成、完整评分、与 release eval **同一数据集和 manifest** 的 run；复用已评测 Version，审计只称五样本有限证据。公开/token 的该路径继续拒绝，待受众专属质量规则，不扩大受众。

真实 Docker 暴露并修复了两处数据库合同错误：预发布 Version Preview 原受旧 shape CHECK 拒绝（epoch 8）；reviewer 随后发现 NULL/UNKNOWN 漏洞（epoch 9）；有 Hosted 历史会话时旧 composite FK 又阻止 Publication 指针回滚（epoch 10）。三个均为前向 authority 迁移，旧 epoch 未改。普通 Preview SSE 断开改为同一持久 Runtime 游标的后台终态重读，避免把断开误报取消；该补写在 Gateway 冷重启后仍无持久重试。

本次实测详情与每个 run 的区分见 [固定 Version 本地实机补验](r3-typed-agent-live-2026-09-28.md)。J14 的 r1 运行中保存 r2、刷新后开独立预览 **Docker Playwright 1/1 通过**；J15 在旧 v2 Hosted 会话存在时回滚 v1，首次 500 后应用 epoch 10，同用例复测 **1/1 通过**。五样本真实 A/B 均经 Runtime/provider 执行；最终 A 有 2 个 `stream_interrupted` 未评分，B 执行 5/5 但预设行为断言 0/5，质量 gate 拒绝，比较标记证据不足。带 KB 来源失败样本的显式重试返回 409，无新 run。不能把这些负向结果写成完整 J16～J18 通过。

独立只读 reviewer 的数据集错配、Web selected run 缺口、Preview detach Trace、shape NULL 漏洞均已最小修正并复核；epoch 9/10 最终静态复核无合入硬阻塞。Codex 内置浏览器仍停留登录页，账号输入受该浏览器安全策略限制；普通 Docker Playwright 使用原专用账号成功。真实裁判故障、五类行为全部通过的固定 A/B、Trace 冷重启补偿、完整双角色/公开受众矩阵仍未验收，**R3 不标完成**。

## 2026-09-28 最后一轮安全增量收口

用户本轮明确授权提交、推送并合入 `main`，替代上文当时的“未授权推送”状态。本段记录基于 `main@f733eb5f` 的新增差异；历史验收结果保持原样。

- 修复固定 Version 的 V2 和旧版预览/已发布入口首次建 Runtime Thread 时的指令绑定；仅 resume 传入指令时，已加载 Thread 可能忽略覆盖。新增旧版 preview/published 首轮请求回归。
- 内部 linked publish 只接受已选定、同数据集及 manifest、且 Eval gate 明确通过的 run；失败 gate 在发布页显示不可用于关联。A/B 相同的关键失败继续阻断比较，裁判失败后候选执行成功仍显示为成功执行且未评分。已接受的安全重试幂等回放返回原回执。
- 新增只读持久 Trace 补账：Gateway 冷重启后从原 run、原 turn 终态事件回填一条 Trace 与一条 outbox，不启动模型或工具。reviewer 找到 Rust 取消持久事件 `compat/v1/cancelled` 遗漏，已补终态查找、校验、事务约束和回归。

| 证据层 | 本轮实际结果 |
| --- | --- |
| 自动化 | 受影响 8 个 Python 文件 127/127；取消补账 10/10、旧版固定 Version 首轮请求 3/3；Ruff、Web app/node TypeScript、lint、build、i18n、`make harness-check` 通过。`make eval-e1-gate`、`make agent-eval-core-gate`、`make verify-eval-dev`、`make architecture-boundary-gate`、`make verify-openapi-contract` 退出 0。 |
| 本地 Docker | Owner labels 指向本仓库；`make hot-update ARGS=--gateway` 和 `--frontend` 后 `make status` 全健康；Gateway 补账源与容器、前端 `dist/index.html` 与容器 SHA-256 一致。旧失败 run `ad46d3d7-7216-4c5e-a830-8b15d28ce9fb` 冷重启后为 Trace 1、outbox 1、模型调用 1、原事件 1248，没有重放。 |
| 浏览器 | 发布页 Playwright 11/11；开启 live 开关后现有 E2E 账号在 Docker 上发布/回滚 1/1。Codex 内置浏览器本轮仍在 `/login`，认证后的交互未验；不能把 Playwright 算作 IAB。 |
| 独立 review | reviewer 提出的取消事件和旧版首轮指令两个确定缺口已修并复核；最终静态复核未发现剩余确定合入阻塞。扫描终态 run 缺少匹配时间排序的索引，是未压测的规模风险，未伪称已验证性能。 |

仍未把 J16～J18 的五类行为全部跑出可发布质量证据；真实裁判/provider 故障、双角色/公开受众矩阵与内置浏览器登录后流程未在本轮完整验收。故该提交是可合入安全增量，**完整 R3 保持进行中**。
