# Assistant Runtime 可信化：目标与明细计划

> 文档类型：目标、验收标准、执行边界、明细工作项
>
> 基线：`feat/local-os-agent-and-stream-contract@0757181`（含 `main@e0983ce` 之后的全部工作树改动）
>
> 与 `assistant-local-os-product-contract.md` 的关系：**互补，不重叠**。那份文档定义 Local OS 的
> *产品能力边界*；这份文档定义运行时的 *可信度欠账*。两者可并行推进，但本文件的 G1/G2 是
> Local OS 正式对外之前的前置条件——在别人的真机上跑失控循环，比在服务端沙箱里跑失控循环
> 严重得多。
>
> 来源：2026-08-12 对 `apps/assistant-service`（117k 行）的证据级审查，含对运行中容器的真实调用采样。

---

## 第一部分：目标（GOAL）

### 需求结果

当前 Assistant 的问题不是"功能不够"，而是**它对自己的描述不可信**。具体表现为三类，全部有实测证据：

1. **配置真值不唯一**——同一个开关在代码默认值、`.env.example`、`docker-compose.yml`、
   容器实际环境这四处各说各话，运行态无法从任何一份配置推导。
2. **声明的能力未接线**——5 个可靠性中间件写完了、测试全绿、从未注册进生产中间件链。
3. **失败不可诊断**——内部异常只记录 `exception_type`，不带 message 与 traceback。

因此本轮的最终结果定义为：

> **Assistant 运行时的每一项对外声明，都能被一条机器可执行的命令证伪。**

这不是"提高代码质量"这类无法验收的目标。它可以拆成五条硬指标（G1–G5），每条都有明确的
通过/不通过判定。

### 验收标准

| 编号 | 目标 | 通过判定（必须可机器执行） |
|---|---|---|
| **G1** | 长任务不失控 | 构造一个会重复调用同一工具的场景，`LoopDetectionMiddleware` 实际拦截并产生可观测事件；构造超时场景，`TimeBudgetMiddleware` 实际拦截。两者均在**默认**中间件链上生效，不需要额外配置 |
| **G2** | 失败可诊断 | 任意内部异常在服务端日志中可读到脱敏后的 message 与 traceback；同一异常在 SSE 与 trace 的用户可见字段中**仍然**只有脱敏摘要 |
| **G3** | 配置真值唯一 | 服务启动时输出一份"已解析开关指纹"，包含每个开关的最终生效值与来源；存在一个测试断言 `.env.example` 与代码默认值一致，或对每处差异有显式豁免声明 |
| **G4** | 评测不可伪造 | 用 `src/services/eval/fixtures/general_agent_receipt.example.json` 里的占位摘要（全 `a`／全 `b`）跑评测，**必须失败**；三份仅 trial 号不同的回执，**必须失败** |
| **G5** | 传输开销有上界 | 存在一个测试断言任意单条 SSE 事件的 payload 小于硬上界；超限内容改走 artifact 引用 |

### 边界（不得越界）

**结构性边界**

- 不得新建第二条执行路径。canonical AgentLoop / ExecutionGateway / CapabilityAllowlist 仍是唯一
  执行面，Local Node 与 OpenAI Responses 都是其 adapter。
- 不得放宽任何既有的审批、租户隔离、能力白名单或脱敏行为。任何一项如需变更，单独提出。
- 不得修改 `turn_event_collector` 对 terminal envelope 的逐字节一致性校验。该不变量在本次
  turn_state 精简中拦下了一版过宽的实现，是有效的护栏。

**过程性边界**

- **不得为了让测试变绿而修改测试断言。** 断言与实现冲突时，先判定哪一边是对的，并在提交信息
  里写明理由。
- **不得记录未实际运行的验证。** 每条"已验证"必须附带：执行的命令、通过/失败计数、以及每个
  失败项的归因。
- **声称修复某问题前，必须先有一个能复现该问题的失败测试。** 没有红过的测试，不能证明它变绿
  是因为你的改动。
- 每项改动必须能被独立回滚，不与其他项耦合成一次大提交。

**协作边界（与本仓库另一个 agent 并发工作时）**

- 状态还原只用 `git`（`git stash` / `git checkout`），**不要用文件拷贝做快照**。本次审查中，用
  `cp` 备份并还原一个正被并发编辑的文件，覆盖了对方的新改动，造成工作树不一致并凭空产生
  81 个失败，一度被误判为代码缺陷。
- 改动他人正在编辑的文件时，把改动压到最小的单个 hunk。

---

## 第二部分：明细计划

按优先级排列。P0 是"当前主干上就存在的确定缺陷"，P1 是"Local OS 对外前的前置条件"。

---

### P0-1　修复 `attachment.deleted_at` 列不存在

**问题**　`agent_repository.py:5314` 与 `:7370` 查询 `attachment.deleted_at`，但
`agent_runtime_attachments`（`database/migrations/080_agent_channel_delivery_hardening.sql:24`）
没有该列，全库也没有任何迁移创建它。命中即 `asyncpg.exceptions.UndefinedColumnError`。

**证据**　这是当前唯一的离线测试失败项：

```
tests/database/test_agent_channel_runtime.py::test_real_postgres_idempotency_is_atomic_and_replays_terminal_result
E   asyncpg.exceptions.UndefinedColumnError: column attachment.deleted_at does not exist
```

该缺陷**先于本轮改动存在于 main**（两个文件在 `0757181` 中均为 0 改动）。

**需要先做的决策**　该表用 `expires_at` 做 TTL，没有软删除概念。两种修法语义不同，会改变租户
配额计算，必须先定：

- (a) 删除 `AND attachment.deleted_at IS NULL` 谓词 → 过期附件仍计入存储配额
- (b) 改为 `AND attachment.expires_at > NOW()` → 只有未过期附件计入配额
- (c) 新增 `deleted_at` 列并实现软删除 → 需要迁移，且要说明谁写这个字段

倾向 (b)：它最接近原谓词的意图（"只算活着的附件"），且不需要迁移。但这是产品决策，不该由
实现者默认。

**验收**　配置了真实 Postgres 后全量离线套件 0 失败；且存在一个测试，在谓词被改回错误列名时
会失败。

**边界**　若选 (c)，迁移必须单独提交并附回滚脚本；不得在同一提交里混入其他改动。

---

### P0-2　评测反造假（G4）

**问题**　Codex 自己 2026-08-12 的审计
（`reports/code-review/GENERAL_AGENT_EVAL_ANTI_INFLATION_AUDIT_2026-08-12.md`）已判定
**REQUEST CHANGES**，列出两个 P0，需要落地：

1. 任意回执 JSON 被当作执行证据——`content_sha256` 可选，且仅校验是否为 SHA-256 字面格式，
   从不由 host 重算。checked-in 的示例回执用全 `a`／全 `b` 占位摘要即可通过。
2. 重复次数不独立——回执加载器只拒绝重复的 `(case_id, trial)`，不拒绝复用 `attempt_id`、
   证据 ID、内容摘要或 provider completion ID。三份仅 trial 号不同的回执即可通过一个
   critical 3/3 用例。

**验收**　两条都必须是**先红后绿**的测试：

- 用占位摘要的示例回执跑评测 → 断言失败
- 三份 `attempt_id` 相同的回执 → 断言失败
- host 从实际字节重算 `content_sha256`，与回执声明不符时 → 断言失败

**边界**　不得放宽任何现有 gate 来让新测试通过。评测分数的口径变化必须在提交信息中写明——
分数下降是预期结果，不是回归。

**为什么是 P0**　当前任何基于评测分数的发布判断都不成立。在这条修好之前，`HANDOFF.md` 里
"所有声明特性通过"这类结论不应被引用。

---

### P1-1　接线可靠性中间件（G1）

**问题**　`core/agent/middlewares/harness.py` 提供 5 个中间件，文件顶部写着
"intentionally not registered by default"，全仓库只有测试引用它们：

| 中间件 | 作用 | 生产引用 |
|---|---|---|
| `CallLimitMiddleware` | 单轮工具调用上限 | 0 |
| `LoopDetectionMiddleware` | 重复相同工具调用检测 | 0 |
| `TimeBudgetMiddleware` | 墙钟预算 | 0 |
| `PreCompletionChecklistMiddleware` | 空回复转为可恢复错误 | 0 |
| `TraceSensorMiddleware` | 流/错误观测 | 0 |

生产链（`agent_loop.py:_build_default_middleware_chain`）只注册
`RuntimeMemory` / `ToolOutputSpill` / `ResponseCap` 三个。

**为什么现在做**　Local OS 让 agent 在用户真机上执行文件与进程操作。失控循环在服务端沙箱里是
浪费 token，在别人的 Mac 上是另一回事。这是 Local OS 对外的前置条件。

**验收**

- 5 个中间件注册进默认链，阈值经配置项暴露
- 结果级证明，不是单元测试：构造一个真实会重复调用同一工具的对话，抓 SSE 流，证明拦截发生
  且产生了可观测事件
- 默认阈值必须宽到不影响现有任何通过用例——接线前后全量套件失败数不变

**边界**

- `TraceSensorMiddleware.on_error` 里的 `if False: yield None` **不要"修"**。它是让该函数成为
  async generator 的惯用写法，`MiddlewareChain.run_on_error` 用 `async for` 迭代它。删掉会
  直接坏掉。可以换成更清晰的写法，但必须保持 async generator 语义。
- 阈值必须可按租户/执行档位覆盖，不能写死。

---

### P1-2　失败可诊断（G2）

**问题**　`core/agent/streaming_execution.py:541`：

```python
logger.error("[STREAMING-FIRST] Error (exception_type=%s)", type(e).__name__)
```

只有异常类名，没有 message，没有 traceback。本次审查中，一个 `AttributeError` 因此无法定位，
最后靠临时给这行加 `exc_info=True` 才查出来。生产环境遇到同样情况会更贵。

**验收**　服务端日志可读到脱敏后的 message 与 traceback；同一异常在 SSE `error` 事件与 trace 的
用户可见字段中**仍然**只有脱敏摘要（现有 `_redact_trace_text` 行为不变）。新增一个测试，断言
原始异常文本不出现在对外 payload 中。

**边界**　这是"对内可见、对外仍脱敏"。绝不能因为要方便调试就把原始异常文本泄漏到 SSE、trace
的用户可见字段或 artifact 里。

---

### P1-3　配置真值唯一（G3）

**问题**　最能说明问题的是 `ASSISTANT_SUBAGENTS_ENABLED`，2026-08-12 实测：

| 声明处 | 值 |
|---|---|
| 代码 `_env_truthy("ASSISTANT_SUBAGENTS_ENABLED")`（`main.py:67`，无默认参数 → 缺失即 false） | `false` |
| `.env.example` | `false` |
| `docker-compose.yml:538` `${ASSISTANT_SUBAGENTS_ENABLED:-false}` | `false` |
| **`docker exec ai-gateway-assistant-service printenv`** | **`true`** |

三处声明一致地说 `false`，运行中的实例是 `true`（来自未入库的真实 `.env`）。**运行态无法从任何
一份可读的声明推导出来，只能去问容器。** 这正是本项要解决的问题。

（`ASSISTANT_RUNTIME_SKILLS` 曾有同类漂移，代码默认 `False` vs `.env.example` `true`；核对时发现
代码默认已被改为 `True`，三处现已一致，无需处理。此处记录以免重复排查。）

**验收**

- 服务启动时输出一份"已解析开关指纹"：每个开关的最终生效值 + 来源（env / 代码默认 / 租户覆盖），
  并纳入 trace，使任何一次运行都能回答"当时到底开着什么"
- 存在一个测试，断言 `.env.example` 与代码默认值一致；对每处有意的差异，要求显式豁免声明
- 指纹中的密钥类配置只输出是否已设置，不输出值

**边界**　**本项不改变任何开关的当前生效值**，只让它变得可见且一致。如果对齐过程中发现某个
默认值本身就该改，单独提出，不要夹带。

---

### P2-1　传输开销上界（G5）

**背景**　本轮已修复 turn_state 的二次增长（每事件恒定 ~450 B，单轮 128 KB → 32 KB）。
剩余最大单项是 `context_budget` 事件：单条 8.7–10 KB 的 context_packet 全量转储，一轮两条
就近 20 KB。

**验收**　存在一个测试断言任意单条 SSE 事件 payload 小于硬上界（建议 4 KB，具体值由实测定）；
超限内容改走 artifact 引用，事件里只留 ID。

**边界**　不得因此丢失可审计性——被移走的内容必须仍可通过 artifact ID 完整取回。

---

### P2-2　工具发现开销：先量化，再决定

**观察**　实测中，让 Assistant "调用 todo_write 再调用 todo_read"，它先花了 4 次工具调用做
`tool_search` × 2 + `tool_describe` × 2，才开始真正工作。

**这不一定是缺陷**——动态工具发现（primitives 模式）本来就是用调用次数换上下文体积。但目前
没有数据支撑这个取舍。

**要求**　先量化再决定，不要直接改：

- 测量在当前工具规模下，`tool_search`/`tool_describe` 的往返成本 vs 直接暴露全部 schema 的
  上下文成本
- 给出一个阈值：工具数少于 N 时直接暴露，超过 N 时走发现模式
- 结论写进文档，无论是否改代码

**边界**　在拿到数据之前不要改动工具暴露策略。

---

## 第三部分：证据标准

这一节针对本项目已发生过的具体问题：文档描述的系统比实际存在的更完整。

**"已验证"必须包含**

1. 实际执行的命令（可复制粘贴）
2. 通过 / 失败 / 跳过 的计数
3. 每个失败项的归因结论——是既有缺陷、是环境问题、还是本次改动引入

**不接受的证据**

- "非空输出"当作能力成功。这条在 `HANDOFF.md` 里已经写过，仍需重申。
- 评测分数，在 P0-2 修好之前。
- 声称跨平台能力，而只有单平台 live 证据。
- 引用一份未随代码同步更新的文档结论。

**并发工作时的额外要求**

本仓库当前有多个 agent 并发编辑。报告失败数时，必须先归因再下结论：用 `git stash` 取得真基线，
或只回退自己的 hunk 后重跑。本次审查中曾出现 9 → 92 → 4 的失败数波动，其中 92 那次完全是
还原方式错误造成的假象，与任何代码缺陷无关。

---

## 附：当前基线

```
分支    feat/local-os-agent-and-stream-contract@0757181
离线    5983 passed, 1 failed, 37 skipped
        唯一失败 = P0-1（main 既有缺陷，需真实 Postgres 复现）
接口    热更新至容器后实测：算术正确、多工具生命周期完整闭环、无 run_error
lint    183 个改动/新增文件全绿；仓库其余 974 项为既有，本项目无全仓 ruff 门禁
```

复现命令：

```bash
.venv/bin/python -m pytest tests/ -q --no-header --no-cov --ignore=tests/integration
```
