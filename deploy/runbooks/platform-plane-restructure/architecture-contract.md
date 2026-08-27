# PPR 架构契约

> 本程序执行期间不可漂移的不变量。任何阶段若要违反其中一条，**停下来说明**，不要绕开。
> 与 [`docs/harness/architecture.md`](../../../docs/harness/architecture.md) 和
> [`docs/harness/platform-architecture.md`](../../../docs/harness/platform-architecture.md) 冲突时，以那两份为准。

---

## 1. 五个 plane 及其 SLO 类别

| Plane | 职责 | SLO 类别 | 语言取向 |
| --- | --- | --- | --- |
| **Edge** | 鉴权 · 准入 · 限流 · 路由 · 配额判定 | p99 附加延迟 ≤ 10 ms；永不阻塞 | 由 PPR-03 实测决定 |
| **Control** | Agent/Thread 生命周期 · 能力目录与指纹 · 策略 · Studio · 计费 | p95 ≤ 200 ms；不在 token 路径 | Python |
| **Data** | Agent kernel · 模型面流式 · 能力执行 | 每 token 附加开销有界；每流 RSS 有界 | Rust |
| **Index** | 摄入（CPU/批量）⟂ 检索（IO/交互） | 摄入不得抬高检索 p99 > 10% | Python + 原生内核 |
| **Storage** | PostgreSQL · Redis · Qdrant · 对象存储 | — | — |

---

## 2. 硬规则

**H1 — 数据面在 token 路径上不得同步调用控制面。**
授权与目录随 turn 快照下发（ADR-007 已有机制）。当前 `capability-worker → knowledge-service` 是 2 跳且知识侧本地验 HMAC proof，不回调网关——**这个性质必须保持**。

**H2 — plane 之间只走既有版本化信封。**
`agent-event/v2`、`ai-platform-capability-contract/v2`、公共 OpenAPI。不新增自定义协议（`platform-architecture.md` L2）。

**H3 — 治理面不得与执行面同进程。**
Eval / 审计 / trace 消费不得与边缘或数据面共享事件循环。

**H4 — 业务逻辑不得进入 `packages/ai-gateway-core/`，也不得以 Rust 形式重演该耦合。**
（`platform-architecture.md` L3、§4）

**H5 — 依赖方向不变。**
`web → src → apps/* → packages/ai-gateway-core`。`apps/*` 不得 import `src/`；app 之间不得互相 import；core 是叶子。由 `make test-isolation` 强制。

**H6 — 公共契约零变更。**
`sdk/openapi.json`、事件信封、Capability Contract V2、四端 SDK 行为。任何变更都不属于本程序。

**H7 — Agent Runtime 保持隔离运行时家目录。**
不得继承宿主指令、插件、MCP、凭据或文件系统状态（`architecture.md` §1）。

**H8 — 供应链身份必须单一权威。**
镜像身份的权威是 `deploy/agent-runtime-source/lock.json`；其余位置（`.env`、`.env.example`、compose）必须由工具从 lock 派生，**不得手改**。PPR-01 的交付物之一就是让这条成立。

---

## 3. 不得触碰

| 项 | 原因 |
| --- | --- |
| `deploy/agent-runtime-source/` 的锁与 SBOM 语义 | 供应链证明链；只能用工具改，不能手改 |
| `rust/agent-runtime-overlay/` 的上游派生关系 | overlay 是 openai/codex 的受控叠加；不得就地分叉 |
| `assistant_runtime_*` 审计链的 `ON DELETE RESTRICT` | 留存要求；删除路径必须墓碑化 |
| `agent-plugins/`、`skills/`、`sdk/` | 平台**产品交付物**，不是本程序的工具 |
| 能力审批策略（`on-request` + `sandbox: read-only`） | 安全边界 |

---

## 4. 每阶段必过的回归

| 门禁 | 命令 |
| --- | --- |
| 依赖方向与隔离 | `make test-isolation` |
| Harness 契约 | `make harness-check` |
| 供应链 | `make agent-runtime-source-contract` |
| 退役依赖未回流 | `make runtime-dependency-gate` |
| 四端 SSE 信封 | `make sdk-sse-contract` |
| Python 全量 | `uv run --all-packages --extra test pytest -q --no-cov tests packages/*/tests` |
| 真机浏览器 | `E2E_BASE_URL=http://localhost:8081 E2E_API_URL=http://localhost:8080 pnpm exec playwright test -c playwright.live.config.ts --workers=1` |

真机套件的基线是 **141 passed / 0 failed / 5 skipped**（2026-08-26）。低于此即为回归。

---

## 5. 证据要求

每阶段在 `reports/platform-plane-restructure/` 下留一份，必须包含：

1. 动手**之前**复跑的基线数字
2. 该阶段门禁的目标值（动手前写死，不得事后调整）
3. 实测结果，含未达标项
4. **被取消的子项与原因**
5. 回滚方式

没有第 4 项的报告视为未完成——本程序显式期待有子项被测量否决。
