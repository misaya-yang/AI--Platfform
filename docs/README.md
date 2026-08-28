# Documentation Index

Everything an agent or a new engineer needs is reachable from this page. If knowledge is not in
this repository, it does not exist — see [`harness/README.md`](harness/README.md) §1.

**Updated:** 2026-08-28

---

## Start here

| You are | Read |
| --- | --- |
| An AI agent about to change code | [`AGENTS.md`](../AGENTS.md), then [`harness/`](harness/README.md) |
| Using Claude Code specifically | [`CLAUDE.md`](../CLAUDE.md) |
| Running the project for the first time | [`README.md`](../README.md) → `make doctor` → `make quickstart` |
| Deploying to a server | [`DEPLOY.md`](../DEPLOY.md) |
| Contributing a change | [`CONTRIBUTING.md`](../CONTRIBUTING.md), [`harness/workflow.md`](harness/workflow.md) |
| Reporting a vulnerability | [`SECURITY.md`](../SECURITY.md) |

## Layout

| Directory | Holds | Lifecycle |
| --- | --- | --- |
| [`harness/`](harness/README.md) | The agent harness: contract, architecture, commands, workflow | Living — must stay true, gated by `make harness-check` |
| [`architecture/`](architecture/) | ADRs — one file per boundary decision | Immutable once accepted; superseded by a new ADR |
| [`design/`](design/) | Durable design intent that outlives any one plan | Living |
| [`plans/`](plans/) | Plans for work that is scoped but not yet a program | Deleted or archived once delivered |
| [`research/`](research/) | External comparisons and background reading | Dated; archived when stale |
| [`archive/<yyyy-mm>/`](archive/) | Superseded snapshots kept for provenance | Never edited; read-only history |
| [`../deploy/runbooks/`](../deploy/runbooks/) | Multi-session programs; `loop-state.json` is authoritative | Living while active, then terminal |
| [`../reports/`](../reports/) | Evidence: reviews, benchmarks, regression output | Append-only, dated |
| [`../web/e2e/`](../web/e2e/README.md) | The only home for Playwright specs, helpers, and fixtures | Living |

Rule of thumb: **design** says why, **plans** say what next, **runbooks** track a program in flight,
**reports** are evidence of something that happened, **archive** is what used to be true.

## Harness

| Doc | Purpose |
| --- | --- |
| [`harness/README.md`](harness/README.md) | What the harness is, the five principles, how to change it |
| [`harness/platform-architecture.md`](harness/platform-architecture.md) | The product law: kernel / contract / surfaces / extensions, and the admission table |
| [`harness/architecture.md`](harness/architecture.md) | Topology, module ownership, dependency direction, contracts that must not drift |
| [`harness/commands.md`](harness/commands.md) | Canonical command for every routine action; which gate to run for what |
| [`harness/workflow.md`](harness/workflow.md) | Task loop, definition of done, program convention, git rules |
| [`harness/runtime-and-secrets.md`](harness/runtime-and-secrets.md) | Mandatory before any Docker, deploy, or E2E action |

## Design

| Doc | Subject |
| --- | --- |
| [`design/agent-design-manifesto.md`](design/agent-design-manifesto.md) | Design intent for the general agent runtime |
| [`design/general-agent-product-contract.md`](design/general-agent-product-contract.md) | Product, UX, safety, eval, and release contract for the general Agent |
| [`design/agent-design-manifesto-critique.md`](design/agent-design-manifesto-critique.md) | Counter-arguments against the manifesto; read with it, not instead of it |
| [`design/agent-optimization-spec.md`](design/agent-optimization-spec.md) | Optimization targets for the agent loop |

## Architecture decisions

| ADR | Subject |
| --- | --- |
| [`ADR-004`](architecture/ADR-004-bounded-plugin-subagent-delegation.md) | Bounded plugin subagent delegation |
| [`ADR-005`](architecture/ADR-005-model-capability-profiles.md) | Tenant-configurable model capability profiles |
| [`ADR-006`](architecture/ADR-006-agent-runtime-single-kernel.md) | Agent Runtime as the single target Agent kernel |
| [`ADR-007`](architecture/ADR-007-agent-runtime-data-boundaries.md) | Gateway model plane, ThreadStore, and capability-service boundaries |

Write a new ADR when a change alters a dependency boundary, adds a service, or changes a contract
listed in [`harness/architecture.md`](harness/architecture.md) §4.

## Plans

| Plan | Subject |
| --- | --- |
| [`plans/sota-performance-optimization-2026-08.md`](plans/sota-performance-optimization-2026-08.md) | **active** — 核心微服务 SOTA 性能优化（网关 / 助手 / 知识 / Web / 数据面）。证据：[`reports/performance/sota-microservice-review-2026-08-17.md`](../reports/performance/sota-microservice-review-2026-08-17.md)。剩余阶段已按交接提示词实施（[`reports/performance/sota-spo-remaining-2026-08.md`](../reports/performance/sota-spo-remaining-2026-08.md)）；现行性能程序为 `deploy/runbooks/sota-performance-dual-gate/`。 |
| [`plans/sota-performance-claude-handoff.md`](plans/sota-performance-claude-handoff.md) | Claude Code 交接提示词：做完剩余 SPO 阶段；Grok 按文内清单 review/测试 |
| [`plans/assistant-upgrade-plan-2026-08.md`](plans/assistant-upgrade-plan-2026-08.md) | **superseded** — Assistant runtime upgrade snapshot retained for measured evidence; current direction is the lighten plan below. |
| [`plans/assistant-harness-lighten-plan-2026-08.md`](plans/assistant-harness-lighten-plan-2026-08.md) | Reducing assistant harness weight |
| [`plans/kb-rag-optimization-plan.md`](plans/kb-rag-optimization-plan.md) | KB retrieval quality / UX（不要与运行时性能计划混淆） |
| [`plans/rust-expansion-and-service-topology-2026-08.md`](plans/rust-expansion-and-service-topology-2026-08.md) | **plan** — 执行面 Rust 化之后：迁移判据、平面拓扑、业界 SOTA 对照。实施拆解见 `deploy/runbooks/platform-plane-restructure/` |
| [`plans/knowledge-bm25-v2-shadow-rollout.md`](plans/knowledge-bm25-v2-shadow-rollout.md) | BM25 v2 shadow rollout |

## Research

- [`research/ai-assistant-harness-4oss-comparison-research-2026-08.md`](research/ai-assistant-harness-4oss-comparison-research-2026-08.md) — comparison against four open-source assistant harnesses.

## Programs

All multi-session programs live in [`../deploy/runbooks/`](../deploy/runbooks/). Status comes from
each program's `loop-state.json`, never from prose. Verified 2026-08-28:

| Program | Terminal phase | Status |
| --- | --- | --- |
| **`platform-plane-restructure`** | PPR-00 → PPR-09 | **PPR-00 complete** — 计时归因、供应商基线与并发资源剖面已冻结；PPR-01 等待用户批准进入。PRD: [`product-requirements.md`](../deploy/runbooks/platform-plane-restructure/product-requirements.md) |
| **`agent-runtime-full-rust-cutover`** | FRC-00 → FRC-06 | **FRC-06 未闭合** — 全面 Rust 切换、验收与回滚演练已在 main 通过；尚差提交/推送回滚证据并标记 FRC-06 done。Python AgentLoop 已删除 |
| **`agent-runtime-single-kernel`** | CHR-00 → CHR-06 | **superseded** — CHR-05 source-lock checkpoint 后由 `agent-runtime-full-rust-cutover`（Rust kernel 迁移）接替；无新 PRD 不得重启。运行流量从未移动。 |
| **`performance-correctness-hardening`** | PCH-00 → PCH-07 | **active** — PCH-07 is the stop-safe tool-exchange gate. 后续性能工作：计划在 [`plans/sota-performance-optimization-2026-08.md`](plans/sota-performance-optimization-2026-08.md)，现行程序为 `sota-performance-dual-gate`（见下表）。 |
| **`sota-performance-dual-gate`** | SPD-00 → SPD-04 | **blocked（SPD-04）** — Grok hardening 批次集成与性能/质量双门禁；SPD-00→03 已过，实现收尾完成。blockers：live TTFT p50 9.28s 超 3.41s gate；三个八任务稳定性 cohort 未跑。 |
| **`agent-contract-unification`** | ACU-00 → ACU-06 | **superseded** — 2026-08-14 scaffold 后从未开工，由 `agent-runtime-full-rust-cutover` 接替；无新 PRD 不得重启。原目标：assistant 成为 `AgentSpec` 实例、公共 runtime API 为唯一面契约（见 [`harness/platform-architecture.md`](harness/platform-architecture.md)） |
| **`product-convergence`** | PC-04 | **verified** — review findings #1–#5+#8: first-run onboarding, connectors as the only Confluence story, ai-quiz plugin, nav groups, DEFAULT_MODEL. Report: [`reports/code-review/product-convergence-2026-08.md`](../reports/code-review/product-convergence-2026-08.md) |
| `agent-studio-prd` | AS-09 | verified, no blockers |
| `agent-trace-eval-prd` | ATE-04 | verified, terminal — expansions need a new program |
| `assistant-general-agent-harness` | AGA-06 | 7/7 phases done; superseded by Rust kernel migration |
| `assistant-hermes-runtime-prd` | AHR-05 | passed, requirement chain complete; superseded by Rust kernel migration |
| `assistant-runtime-optimization` | ARO-05 | verified, complete; superseded by Rust kernel migration |
| `eval-quality-optimization` | — | design + plan + verification report, no loop state |
| `agent-kb-eval-optimization-20260802` | — | README only |

Standalone runbooks in the same directory:

- `assistant-runtime-operating-model.md` — runtime health, failure categories, no-go thresholds, rollback.
- `assistant-runtime-trust-hardening.md` — trust boundary hardening.
- `assistant-local-os-product-contract.md` — local OS agent product contract.
- `open-source-env-readiness-todo.md` — open-source release readiness (checked by CI).

## Reports

`../reports/` holds evidence, organised by area: `code-review/`, `benchmark/`, `eval-regression/`,
`agent-studio/`, `assistant-runtime-regression/`, `assistant-local-os/`, `performance/`,
`security/`, `agent-runtime/`.

Latest performance planning evidence: [`../reports/performance/sota-microservice-review-2026-08-17.md`](../reports/performance/sota-microservice-review-2026-08-17.md)
— five-agent read-only review of gateway, assistant, knowledge, web/SDK, and the data plane (2026-08-17).

Current working backlog: [`../reports/code-review/codebase-hygiene-scan-2026-08-13.md`](../reports/code-review/codebase-hygiene-scan-2026-08-13.md)
— dead code, oversized files, and comment drift, worked P0 → P1 → P2. Re-run its `rg` cross-checks
before deleting anything; the report is a snapshot, not a live view.

Latest: [`../reports/code-review/product-convergence-2026-08.md`](../reports/code-review/product-convergence-2026-08.md)
— product-convergence program final verification (findings #1–#5 + #8, connectors, ai-quiz plugin).

## Archive

[`archive/`](archive/) holds superseded snapshots by month. They are provenance, not guidance —
do not act on them without re-verifying against the current code.

| Archived | Why |
| --- | --- |
| `2026-07/PLATFORM_REVIEW_2026-07-14.md` | Platform review superseded by the 2026-08-13 hygiene scan |
| `2026-07/comprehensive-code-review-optimization.md` | Same |
| `2026-07/optimization-report-comprehensive.md`, `optimization-report-grok-build.md` | Point-in-time optimization reports |
| `2026-07/前端UI全面升级优化总纲.md` | Frontend UI upgrade outline, delivered |
| `2026-08/HANDOFF-2026-08-12.md` | Session handoff that depended on a directory outside this repository |
| `2026-08/assistant-module-deep-review-2026-08-12.md` | Superseded by the 2026-08-13 whole-repo scan |

## Writing docs here

1. One topic per file; put it in the directory whose lifecycle matches.
2. Open with what the doc is and when it was last verified.
3. Link to code with repo-relative paths so links survive on every machine.
4. State status plainly — `active`, `superseded by X`, `archived`. A doc with no status decays silently.
5. Update the owning doc in the same change as the code. Add the file to this index if it is new.
6. When a doc stops being true, move it to `archive/<yyyy-mm>/` — do not leave it to be believed.
