# PPR 阶段清单

| 阶段 | 文件 | 依赖 | 交付 | 关键门禁 |
| --- | --- | --- | --- | --- |
| **PPR-00** | `phase-00-separate-local-and-provider-slis-and-freeze-baseline.md` | — | 本地/供应商 SLI 分离；N≥30 方差刻画；回滚包冻结 | 注入 50 ms 本地延迟只移动本地 SLI |
| **PPR-01** | `phase-01-make-rust-build-and-distribution-cheap.md` | PPR-00 | slim 运行镜像；单命令构建链；crate 级 overlay 标识 | 镜像 ≤ 150 MB；改一行 ≤ 15 min；改 worker 不动 runtime tag |
| **PPR-02** | `phase-02-freeze-the-plane-contract-as-an-executable-gate.md` | PPR-01 | ADR-008；`make plane-boundary-gate` | 注入越界 import 后门禁失败 |
| **PPR-03** | `phase-03-extract-the-edge-plane.md` | PPR-02 | 边缘独立部署单元；多 worker | 边缘 p99 ≤ 10 ms；多 worker fuzz 零超卖 |
| **PPR-04** | `phase-04-shadow-and-conditionally-cut-over-the-rust-model-plane.md` | PPR-03 | Rust 模型面影子 → **条件**切流 | SSE fixture 字节一致；50 并发 RSS 降 ≥ 60% 才切 |
| **PPR-05** | `phase-05-slim-the-control-plane-and-split-governance.md` | PPR-04 | 治理面（Eval/trace/审计）拆出 | 重 eval 批不移动流式 p99；网关 RSS 降 ≥ 30% |
| **PPR-06** | `phase-06-give-the-index-plane-capacity.md` | PPR-05 | worker 水平扩展；交互检索 profile；**条件** CPU 内核 | 200 页摄入抬高检索 p99 ≤ 10%；切块边界字节一致才合入 |
| **PPR-07** | `phase-07-unify-session-storage.md` | PPR-06 | 会话历史统一到规范化 item 表 | 旧会话渲染一致；删除仍墓碑化 |
| **PPR-08** | `phase-08-attack-provider-side-latency.md` | PPR-00（**可并行**） | model/variant canary；prompt-cache SLI；thinking 预算 | 达到 3.41 s **或**给出有依据的新门槛 |
| **PPR-09** | `phase-09-release-gate-and-rollback-on-the-new-topology.md` | PPR-07, PPR-08 | 全平面串行发布门禁；数字钉死的回滚演练 | 往返指纹完全一致 |

## 允许负向结论的阶段

| 阶段 | 允许的负向结论 | 为什么这是合格产出 |
| --- | --- | --- |
| PPR-04 | 「已测量，未采纳」——保留 Python 模型面 | 迁移理由是内存而非延迟；内存收益不达标就没有理由 |
| PPR-06 | 「切块内核取消」 | 切块边界漂移会强制整库重嵌入，代价远超收益 |
| PPR-08 | 「3.41 s 门槛不可达」+ 有依据的替代门槛 | 供应商抖动 5.4 s，本地无解；继续追是浪费 |

其余阶段的门禁必须达标，不接受负向结论。

## 每阶段都要跑的回归

见 [`architecture-contract.md`](architecture-contract.md) §4。真机基线 **141 passed / 0 failed / 5 skipped**。
