# 通用智能体对标与缺陷清零：目标与执行计划

> 文档类型：目标、对标方法、验收标准、执行边界
>
> 基线：`feat/local-os-agent-and-stream-contract@0757181`
>
> 与 `assistant-local-os-product-contract.md` 的关系：那份定义 Local OS 的*产品能力*；
> 这份定义*对标方法*与*运行时欠账*。

---

## 第一部分：目标（GOAL）

### 第一性原理

这是一个**企业级专业通用智能体**，对标 Hermes Agent 与 OpenClaw。目标只有三件事：

> **找到所有缺陷 → 升级优化 → 测试回归。**

其中"缺陷"不限于 bug。**能力缺口也是缺陷**——对标对象能完成而我们完不成的任务，就是缺陷。

### 对标对象（本机可直接读源码，不要依赖二手描述）

| 对象 | 路径 | 语言/规模 |
|---|---|---|
| Hermes Agent | `/Users/yang/projects/Hermes_agent` | Python，约 6.5k 源文件 |
| OpenClaw | `/Users/yang/projects/open claw/openclaw` | TS，约 8k 源文件 |

两者都在本机完整可读。**不要用文档宣称或版本说明代替读源码。**

### 需求结果

产出一份**能力差距矩阵**，并把矩阵里的每一项推进到有明确结论的状态。矩阵完成即目标达成。

---

## 第二部分：对标方法

数文件个数、比工具数量**不算对标**。真实对标必须同时做三层，缺一层结论就不成立。

### 第 1 层：任务级对标（最高权重）

选 8–12 个**真实企业任务**，三个系统各跑一遍，比最终交付物，不比事件形状。

任务须覆盖：多步推理、外部信息检索、文件产出、需要澄清的歧义输入、长时任务、失败恢复。

每个任务记录：

- 是否完成（结果级判定，**"非空输出"不算完成**）
- 步数 / 耗时 / token
- 失败时卡在哪一步、什么原因
- 三方结果的质量差异（人工判定即可，但要写明判据）

**这一层是唯一能证明"我们比对方差在哪"的证据。** 架构对比和能力清单都只是解释它的辅助。

### 第 2 层：能力面对标

对每一项对标对象具备的能力，给出**三选一**的结论，不允许留空：

- **已覆盖**——写出我们的对应实现位置
- **采纳**——写出为什么值得做、落到哪个里程碑
- **不采纳**——写出理由。合法理由包括：与企业多租户模型冲突、与审批/隔离边界冲突、目标用户不需要。
  **"没时间"不是理由**，那属于"采纳但排后"。

重点比较维度（Codex 自行读源码展开，不要照抄本文件的猜测）：

- 执行模型：agent loop 形态、中断/恢复、检查点
- 工具体系：工具粒度、动态发现、扩展机制（plugin / skill / MCP）
- 环境控制：浏览器交互、终端/进程、Computer Use、文件系统
- 人机协同：澄清提问、审批、接管、打断
- 会话与记忆：压缩、检索、跨会话
- 多智能体：委派、并行、结果聚合
- 安全：权限模型、沙箱、凭据处理、供应链校验
- 可观测：trace、回放、成本归因

### 第 3 层：架构取舍对标

对标对象是**单用户本地优先**架构，我们是**企业多租户**架构。因此：

- 它们的某些设计我们**不能照抄**（如全主机信任、全局 token、sandbox 默认关闭）
- 我们的某些成本它们没有（租户隔离、审批网关、KB 数据集权限）

这一层的产出是：**明确写出哪些差距是"我们该补的"，哪些是"架构选择的必然代价"。** 把后者
误当缺陷去补，会破坏企业级定位。

### 交付物

一份 `reports/benchmark/agent-capability-gap-YYYY-MM-DD.md`，包含：

1. 任务级对标结果表（含每次运行的原始 trace 或 SSE artifact 引用）
2. 能力差距矩阵（每项有三选一结论）
3. 架构取舍说明
4. 由矩阵推导出的优先级排序，附理由

---

## 第三部分：已确认的运行时欠账

以下是 2026-08-12 审查中**已实测确认**的缺陷，不需要再对标即可动手。它们与对标并行推进。

### P0-1　接线可靠性中间件

`core/agent/middlewares/harness.py` 提供 5 个中间件——`CallLimit`、`LoopDetection`、
`TimeBudget`、`PreCompletionChecklist`、`TraceSensor`——文件顶部写着 "intentionally not
registered by default"，**生产引用数 0**，只有测试引用。生产链
（`agent_loop.py:_build_default_middleware_chain`，`chain.add` 共 3 处）只有 `RuntimeMemory` /
`ToolOutputSpill` / `ResponseCap`。

失控循环在服务端沙箱里是烧 token，在 Local OS 场景下是在用户真机上反复改文件、反复调外部服务。

**验收**　5 个注册进默认链，阈值可配、可按租户/执行档位覆盖；**结果级证明**——构造真实会重复
调用同一工具的对话，抓 SSE 流证明拦截发生；默认阈值宽到接线前后全量失败数不变。

**边界**　`harness.py:140` 的 `if False: yield None` **不要"修"**。它让该函数成为 async
generator，`MiddlewareChain.run_on_error` 用 `async for` 迭代，删掉会直接坏。可换写法，但须保持
async generator 语义。

### P0-2　异常可诊断

系统性模式。实测：`apps/assistant-service/src/` 中 `"exception_type=%s"` 记录点 **116 处**，
其中带 `exc_info` 的 **0 处**。典型形态（`core/agent/streaming_execution.py:547`，主流式循环兜底
handler）只记录类名，无 message、无 traceback、无位置。

代价实测过：本次审查中一个 `AttributeError` 完全无法定位，靠临时加 `exc_info=True` 才查出——
那还是在本地、有源码、能改代码重跑的条件下。

**验收**　服务端日志可读脱敏后的 message 与 traceback；SSE 与 trace 的用户可见字段**仍然**只有
脱敏摘要；测试断言原始异常文本不出现在对外 payload。

**边界**　"对内可见、对外仍脱敏"。不得把原始异常文本泄漏到 SSE、trace 用户可见字段或 artifact。
建议抽一个记录辅助函数统一替换，不要 116 处手改。

### P1-1　配置真值唯一

`ASSISTANT_SUBAGENTS_ENABLED` 实测：代码 `_env_truthy`（`main.py:67`，无默认参数→缺失即
false）、`.env.example`、`docker-compose.yml:538` 三处一致写 `false`；
`docker exec ai-gateway-assistant-service printenv` 是 **`true`**。运行态无法从任何一份可读声明
推导，只能问容器。

**验收**　启动时输出"已解析开关指纹"（每开关最终生效值 + 来源），纳入 trace；密钥类只输出
是否已设置；测试断言 `.env.example` 与代码默认值一致，有意差异需显式豁免。

**边界**　本项**不改变任何开关的当前生效值**，只让它可见且一致。

### P2-1　`attachment.deleted_at` 列不存在

`agent_repository.py:5314` 与 `:7370` 查询 `attachment.deleted_at`，但
`agent_runtime_attachments`（`database/migrations/080_...sql:24`）无该列，全库无迁移创建它。
这是 **main 上的既有缺陷**（两文件在 `0757181` 中均 0 改动），也是当前唯一离线测试失败项。

需先定语义（会改变租户配额计算）：(a) 删谓词，过期附件计入配额；(b) 改 `expires_at > NOW()`，
只算未过期；(c) 新增列并实现软删除，需迁移。倾向 (b)，但属产品决策。

### P2-2　单事件传输上界

本轮已修掉 turn_state 的二次增长（每事件恒定 ~450 B，单轮 128 KB → 32 KB）。剩余最大单项是
`context_budget`：单条 8.7–10 KB 的 context_packet 全量转储。

**验收**　测试断言任意单条 SSE 事件 payload 小于硬上界；超限走 artifact 引用，事件只留 ID，
且内容仍可通过 ID 完整取回。

---

## 第四部分：边界

**结构性**

- 不得新建第二条执行路径。canonical AgentLoop / ExecutionGateway / CapabilityAllowlist 是唯一
  执行面，Local Node 与 OpenAI Responses 都是其 adapter。
- 不得放宽既有的审批、租户隔离、能力白名单或脱敏行为。
- 不得修改 `turn_event_collector` 对 terminal envelope 的逐字节一致性校验——该不变量在本次
  turn_state 精简中拦下过一版过宽实现。
- **不得为了追平对标对象而牺牲企业级定位。** 单用户本地优先架构的便利，多租户环境下往往是
  越权。

**过程性**

- 不得为了让测试变绿而改测试断言。冲突时先判定哪边对，理由写进提交信息。
- 不得记录未实际运行的验证。每条"已验证"附：命令、通过/失败计数、每个失败项的归因。
- 声称修复前，先有一个能复现该问题的失败测试。
- 每项改动可独立回滚。

**协作（本仓库多 agent 并发）**

- 状态还原只用 `git`，**不要用文件拷贝做快照**。本次审查中用 `cp` 还原一个正被并发编辑的文件，
  覆盖了对方改动，凭空产生 81 个失败，一度误判为代码缺陷。
- 改他人正在编辑的文件时，压到最小的单个 hunk。

---

## 第五部分：证据标准

**"已验证"必须包含**：实际执行的命令、通过/失败/跳过计数、每个失败项的归因（既有缺陷 /
环境问题 / 本次引入）。

**不接受**：把"非空输出"当能力成功；只有单平台 live 证据却声称跨平台；引用未随代码同步更新的
文档结论；用对标对象的 README 宣称代替读它的源码。

**并发工作时**：报告失败数前先归因。本次审查出现过 9 → 92 → 4 的波动，其中 92 那次完全是
还原方式错误造成的假象。

---

## 附：当前基线

```
分支    feat/local-os-agent-and-stream-contract@0757181
离线    5983 passed, 1 failed, 37 skipped
        唯一失败 = P2-1（main 既有缺陷，需真实 Postgres 复现）
接口    热更新至容器后实测：算术正确、多工具生命周期完整闭环、无 run_error
lint    183 个改动/新增文件全绿；其余 974 项为既有，本项目无全仓 ruff 门禁
```

```bash
.venv/bin/python -m pytest tests/ -q --no-header --no-cov --ignore=tests/integration
```
