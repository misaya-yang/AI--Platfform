# 平台平面化重构（PPR）

> **状态：** authored，未开工。`loop-state.json` 是进度权威。
> **创建：** 2026-08-26
> **目标：** 把平台按负载类型重构为五个 plane，在有实测收益且有平价预言机的位置迁 Rust，
> 并把性能治理从「一个混合数字」改为「本地与供应商分开度量、分开设门禁」。
> **零公共契约变更。**

---

## 从哪读起

| 顺序 | 文件 | 作用 |
| --- | --- | --- |
| 1 | [`product-requirements.md`](product-requirements.md) | PRD：问题、门槛指标、范围、风险。**§2.5 列出了不得返工的已修项** |
| 2 | [`architecture-contract.md`](architecture-contract.md) | 不可漂移的不变量 H1–H8、每阶段必过的门禁、证据要求 |
| 3 | [`phase-manifest.md`](phase-manifest.md) | 十个阶段与依赖关系 |
| 4 | `phase-NN-*.md` | 当前阶段的可执行文件 |
| 5 | [`loop-state.json`](loop-state.json) | 进度权威：`active_phase` / `active_feature` / `next_action` |
| 背景 | [`../../../docs/plans/rust-expansion-and-service-topology-2026-08.md`](../../../docs/plans/rust-expansion-and-service-topology-2026-08.md) | 判据与业界 SOTA 对照 |

## 怎么跑一轮

1. 读 `loop-state.json` 的 `active_phase`，打开对应 `phase-NN-*.md`。
2. 按 `loop-contract.json` 的 `cycle` 走：observe → select → execute → verify → record → decide。
3. 门禁全部通过后，写 `reports/platform-plane-restructure/<phase>.md`，更新 `loop-state.json`。
4. 命中 `stop_when` 任一条就停下来说明，不要绕开。

## 三条最容易踩的坑

**1. 不要按 SPO 2026-08-17 的清单返工。**
那份清单已过期。经 2026-08-26 逐条复核：准入原子化、双限流去重、SSE 中间件、JWT 单次验签、collection 缓存、摄入/检索进程隔离**六条已修**；日记重切块、模型边界 deepcopy、MCP 握手、串行工具**四条已随 Python AgentLoop 删除失效**。
要重开其中任何一条，先给出当前树的行号证据。

**2. 不要为延迟迁 Rust。**
四次同配置探针里本地开销稳定在 14–19 ms，TTFT p50 在 3.925 s 与 9.281 s 之间摆动。**供应商抖动是本地全部开销的 360 倍。** 迁移的正当理由是内存包络、尾延迟隔离和供应链确定性——每一条都要在阶段报告里用数字兑现。

**3. 子项被测量否决是合格产出。**
PPR-04（模型面）与 PPR-06（切块内核）都写明了「不达标就不切流/不合入」。没有「被取消的子项」一节的阶段报告视为未完成。

## 依赖图

```
PPR-00 度量分离 ─┬─► PPR-01 构建工程化 ─► PPR-02 平面契约 ─► PPR-03 边缘
                 │                                              │
                 │                                              ▼
                 │                                        PPR-04 模型面（条件）
                 │                                              │
                 │                                              ▼
                 │                                        PPR-05 控制面瘦身
                 │                                              │
                 │                                              ▼
                 │                                        PPR-06 索引面容量
                 │                                              │
                 │                                              ▼
                 │                                        PPR-07 会话存储 ──┐
                 │                                                          ▼
                 └─► PPR-08 供应商侧延迟（可并行）───────────────────► PPR-09 发布与回滚
```

PPR-08 只依赖 PPR-00 的度量工作，**可与 PPR-01…07 并行**——它是唯一能真正移动 TTFT 的杠杆。

## 前置

- FRC-06（Agent 执行面 Rust 化）已收尾。
- 真机基线：141 passed / 0 failed / 5 skipped（2026-08-26）。低于此即回归。
