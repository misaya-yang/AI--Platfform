# Open Source Platform Optimization Harness Phase Manifest

This is the compact index for coding agents. Prefer this file plus the target phase file over loading the whole folder.

## Grep Usage

Find a phase:

```bash
rg -n "PHASE_ID: OSP-XX" docs/open_source_platform_optimization
```

Find all goal prompts:

```bash
rg -n "GOAL_PROMPT:" docs/open_source_platform_optimization
```

Find validation commands:

```bash
rg -n "VALIDATION_COMMANDS:" docs/open_source_platform_optimization
```

Find acceptance gates:

```bash
rg -n "ACCEPTANCE_GATES:" docs/open_source_platform_optimization
```

## Phase Index

| PHASE_ID | File | Depends On | Goal Target | Main Validation | Evidence Output |
| --- | --- | --- | --- | --- | --- |
| OSP-00 | `phase-00-open-source-baseline-audit.md` | none | Establish baseline code, validation, and boundary evidence for Open Source Platform Optimization Harness. | code inspection plus available test discovery | `reports/osp-00-open-source-baseline-audit-report.md` |
| OSP-01 | `phase-01-governance-legal-and-security-trust.md` | OSP-00 | Complete the Governance Legal And Security Trust slice while preserving prior phase contracts and downstream handoff boundaries. | phase validation command plus regression evidence | `reports/osp-01-governance-legal-and-security-trust-report.md` |
| OSP-02 | `phase-02-contributor-experience-and-ci.md` | OSP-01 | Complete the Contributor Experience And CI slice while preserving prior phase contracts and downstream handoff boundaries. | phase validation command plus regression evidence | `reports/osp-02-contributor-experience-and-ci-report.md` |
| OSP-03 | `phase-03-demo-data-documentation-and-developer-experience.md` | OSP-02 | Complete the Demo Data Documentation And Developer Experience slice while preserving prior phase contracts and downstream handoff boundaries. | phase validation command plus regression evidence | `reports/osp-03-demo-data-documentation-and-developer-experience-report.md` |
| OSP-04 | `phase-04-release-distribution-and-community-readiness.md` | OSP-03 | Complete the Release Distribution And Community Readiness slice while preserving prior phase contracts and downstream handoff boundaries. | phase validation command plus regression evidence | `reports/osp-04-release-distribution-and-community-readiness-report.md` |

## Phase Report Index

| PHASE_ID | Required Report |
| --- | --- |
| OSP-00 | `reports/osp-00-open-source-baseline-audit-report.md` |
| OSP-01 | `reports/osp-01-governance-legal-and-security-trust-report.md` |
| OSP-02 | `reports/osp-02-contributor-experience-and-ci-report.md` |
| OSP-03 | `reports/osp-03-demo-data-documentation-and-developer-experience-report.md` |
| OSP-04 | `reports/osp-04-release-distribution-and-community-readiness-report.md` |

## Dependency Flow

```text
OSP-00 Open Source Baseline Audit
  -> OSP-01 Governance Legal And Security Trust
  -> OSP-02 Contributor Experience And CI
  -> OSP-03 Demo Data Documentation And Developer Experience
  -> OSP-04 Release Distribution And Community Readiness
```

## Validation Matrix

| PHASE_ID | Mutates Data | Needs Browser/UI | Needs Agent/LLM Eval | Needs Migration | Needs External Service | Release Blocking |
| --- | --- | --- | --- | --- | --- | --- |
| OSP-00 | no | no | no | no | no | no |
| OSP-01 | no | no | no | no | no | no |
| OSP-02 | no | no | no | no | GitHub Actions only | yes, CI gate |
| OSP-03 | local/dev only | yes | optional assistant smoke | possible seed script only | model providers only through mocked or configured local path | yes, demo gate |
| OSP-04 | no by default | yes for release smoke | yes for assistant release smoke | no production migration without approval | package registries and GHCR only with approval | yes |

## Risk Matrix

| PHASE_ID | Primary Risk | Stop Condition |
| --- | --- | --- |
| OSP-00 | stale plan that ignores current repo facts | stop if source packet contradicts repo evidence |
| OSP-01 | incomplete public trust surface or mismatched license metadata | stop if license choice, project URL, or security policy ownership is unclear |
| OSP-02 | CI drift from documented contributor commands | stop if a workflow requires unavailable secrets or fails before dependency installation |
| OSP-03 | demo seed mutates shared data or hides missing journeys | stop if seed path has no dry-run or cannot prove dynamic routes |
| OSP-04 | publishing or deployment without approval | stop if package registry, GHCR, deployment, migration, or release credentials are required |

## Runtime Artifacts

| Artifact | Path | Agent Rule |
| --- | --- | --- |
| Loop Contract | `docs/open_source_platform_optimization/loop-contract.json` | Follow observe, select, execute, verify, record, decide before claiming progress. |
| Loop State | `docs/open_source_platform_optimization/loop-state.json` | Keep active phase, feature, iteration, status, and next action current. |
| Feature Oracle | `docs/open_source_platform_optimization/feature-oracle.json` | Update only status, evidence, and notes for the feature being worked. |
| Progress Log | `docs/open_source_platform_optimization/progress-log.md` | Append session start/end, validation, and blocker notes. |
| Agent Handoff | `docs/open_source_platform_optimization/agent-handoff.md` | Keep planner, generator, and evaluator notes file-based and brief. |
| Continuity Ledger | `docs/open_source_platform_optimization/continuity-ledger.md` | Preserve phase relatedness, code-summary writeback, and interface boundary decisions. |
| Next Window Prompt | `docs/open_source_platform_optimization/next-window-prompt.md` | Use this to restart work in a fresh context window. |

## Agent Role Handoffs

- Planner role: expand intent into phase contracts and feature-oracle cases without over-specifying implementation details.
- Generator role: execute one phase/feature item, update evidence, and hand off to evaluation.
- Evaluator role: review from files and runtime checks, reject superficial completion, and write actionable findings.
- For small low-risk phases, one agent may play generator and evaluator only after running objective validation commands.

## Delivery Quality Gates

- Each phase is independently executable and verifiable.
- Each phase records inherited evidence, dependency status, and what it unlocks next.
- Each phase uses the smallest requirement-satisfying change; scope expansion must be justified in the report.
- Each phase records test evidence and review evidence, or a blocker.
- The terminal phase or release gate runs whole-demand regression across completed feature-oracle items.
- Runtime files must be sufficient for a fresh agent to resume after context compaction.

## Goal Setup Templates

Use the exact phase file `GOAL_PROMPT` when creating an agent goal. If a phase has dependencies, do not execute it until dependency acceptance gates are met or explicitly waived in the previous phase report.

Example:

```text
Complete OSP-01 Governance Legal And Security Trust for `.` by following `docs/open_source_platform_optimization/phase-01-governance-legal-and-security-trust.md`; stay inside named edit boundaries; finish only after root governance files, project URL review, no-secret review, validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
```

## Shared Agent Rules

- Use the exact phase `GOAL_PROMPT` when starting a goal.
- Open only `READ_FIRST` and `PRIMARY_CONTEXT` before planning.
- Make the smallest requirement-satisfying change.
- Expand edit scope only when a blocker is documented.
- Write test evidence, review evidence, and the phase report before moving on.
- Run whole-demand regression in the terminal phase or release gate.

## External Inputs Checklist

- No external inputs are guaranteed by the scaffold.
- Record missing credentials, dashboards, Figma links, deployment access, migrations, and provider approvals before use.
