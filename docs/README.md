# Documentation Index

Everything an agent or a new engineer needs is reachable from this page. If knowledge is not in
this repository, it does not exist — see [`harness/README.md`](harness/README.md) §1.

**Updated:** 2026-08-14

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
| [`design/agent-design-manifesto-critique.md`](design/agent-design-manifesto-critique.md) | Counter-arguments against the manifesto; read with it, not instead of it |
| [`design/agent-optimization-spec.md`](design/agent-optimization-spec.md) | Optimization targets for the agent loop |

## Architecture decisions

| ADR | Subject |
| --- | --- |
| [`ADR-004`](architecture/ADR-004-bounded-plugin-subagent-delegation.md) | Bounded plugin subagent delegation |

Write a new ADR when a change alters a dependency boundary, adds a service, or changes a contract
listed in [`harness/architecture.md`](harness/architecture.md) §4.

## Plans

| Plan | Subject |
| --- | --- |
| [`plans/assistant-upgrade-plan-2026-08.md`](plans/assistant-upgrade-plan-2026-08.md) | Assistant runtime upgrade |
| [`plans/assistant-harness-lighten-plan-2026-08.md`](plans/assistant-harness-lighten-plan-2026-08.md) | Reducing assistant harness weight |
| [`plans/kb-rag-optimization-plan.md`](plans/kb-rag-optimization-plan.md) | KB retrieval quality |
| [`plans/knowledge-bm25-v2-shadow-rollout.md`](plans/knowledge-bm25-v2-shadow-rollout.md) | BM25 v2 shadow rollout |

## Research

- [`research/ai-assistant-harness-4oss-comparison-research-2026-08.md`](research/ai-assistant-harness-4oss-comparison-research-2026-08.md) — comparison against four open-source assistant harnesses.

## Programs

All multi-session programs live in [`../deploy/runbooks/`](../deploy/runbooks/). Status comes from
each program's `loop-state.json`, never from prose. Verified 2026-08-14:

| Program | Terminal phase | Status |
| --- | --- | --- |
| **`agent-contract-unification`** | ACU-00 → ACU-06 | **active** — makes the assistant an `AgentSpec` instance and the public runtime API the only surface contract. Target law: [`harness/platform-architecture.md`](harness/platform-architecture.md) |
| `agent-studio-prd` | AS-09 | verified, no blockers |
| `agent-trace-eval-prd` | ATE-04 | verified, terminal — expansions need a new program |
| `assistant-general-agent-harness` | AGA-06 | 7/7 phases done |
| `assistant-hermes-runtime-prd` | AHR-05 | passed, requirement chain complete |
| `assistant-runtime-optimization` | ARO-05 | verified, complete |
| `eval-quality-optimization` | — | design + plan + verification report, no loop state |
| `agent-kb-eval-optimization-20260802` | — | README only |

Standalone runbooks in the same directory:

- `assistant-runtime-operating-model.md` — runtime health, failure categories, no-go thresholds, rollback.
- `assistant-runtime-trust-hardening.md` — trust boundary hardening.
- `assistant-local-os-product-contract.md` — local OS agent product contract.
- `open-source-env-readiness-todo.md` — open-source release readiness (checked by CI).

## Reports

`../reports/` holds evidence, organised by area: `code-review/`, `benchmark/`, `eval-regression/`,
`agent-studio/`, `assistant-runtime-regression/`, `assistant-local-os/`.

Current working backlog: [`../reports/code-review/codebase-hygiene-scan-2026-08-13.md`](../reports/code-review/codebase-hygiene-scan-2026-08-13.md)
— dead code, oversized files, and comment drift, worked P0 → P1 → P2. Re-run its `rg` cross-checks
before deleting anything; the report is a snapshot, not a live view.

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
