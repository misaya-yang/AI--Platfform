# Open Source Platform Optimization Harness

**Date:** 2026-06-18

**Owner:** Product/engineering

**Purpose:** Convert the current AI Gateway repository into a contributor-ready, release-ready open-source platform through bounded, verifiable phases.

---

## Harness Intent

Convert the current AI Gateway repository into a contributor-ready, release-ready open-source platform through bounded, verifiable phases. The harness must preserve phase relatedness, code-summary writeback, and evidence handoff across long-running agent sessions.

## Coding Agent Loading Protocol

When assigned a phase goal:

1. Open this `README.md`.
2. Open `phase-manifest.md`.
3. Open `loop-contract.json`, `loop-state.json`, `feature-oracle.json`, `progress-log.md`, `agent-handoff.md`, `continuity-ledger.md`, and `next-window-prompt.md`.
4. Locate the target with:

```bash
rg -n "PHASE_ID: <ID>|GOAL_PROMPT|VALIDATION_COMMANDS|ACCEPTANCE_GATES" docs/open_source_platform_optimization
```

5. Open only the target phase file and files listed in that phase's `PRIMARY_CONTEXT`.
6. Create a plan before editing.
7. Treat `LIKELY_EDIT_PATHS` as the intended write boundary.
8. Complete validation, browser/runtime checks, regression scope, review, compliance gates, rollback notes, evidence output, and acceptance gates before claiming completion.
9. Summarize code facts and boundary decisions back into `source-packet.md` and `continuity-ledger.md`.
10. Update `progress-log.md`, `agent-handoff.md`, the phase report, and only the relevant feature `status`/`evidence` fields in `feature-oracle.json`.
11. Move to the next phase only after dependency gates are met or explicitly waived in a report.

## Long-Running Runtime Protocol

Each fresh session must start from durable files instead of hidden chat context or pre-compaction memory:

- Read `docs/open_source_platform_optimization/progress-log.md` and recent git history before choosing work.
- Follow `docs/open_source_platform_optimization/loop-contract.json`: observe, select, execute, verify, record, then decide whether to continue or stop.
- Update `docs/open_source_platform_optimization/loop-state.json` when the active phase, feature, iteration, decision, or blocker changes.
- Update `docs/open_source_platform_optimization/continuity-ledger.md` when code facts, interfaces, contracts, or handoff boundaries change.
- Run the baseline or smoke check named by the target phase before adding new changes.
- Work on one phase and one feature-oracle item at a time.
- Make the smallest requirement-satisfying change and record any scope expansion in the phase report.
- Mark oracle items `passing` only when evidence points to a command, report, screenshot, trace, or log.
- Leave the repo in a clean, restartable state or document the blocker in the phase report.

## Runtime Artifacts

| Artifact | Purpose |
| --- | --- |
| `docs/open_source_platform_optimization/loop-contract.json` | The control loop: observe, select, execute, verify, record, decide. |
| `docs/open_source_platform_optimization/loop-state.json` | Current phase, feature, iteration, status, last decision, and next action. |
| `docs/open_source_platform_optimization/feature-oracle.json` | End-to-end feature/test oracle. Agents may update status and evidence, not delete cases. |
| `docs/open_source_platform_optimization/progress-log.md` | Chronological progress, current blocker, and clean-state notes for the next session. |
| `docs/open_source_platform_optimization/agent-handoff.md` | File-based planner/generator/evaluator handoff packet. |
| `docs/open_source_platform_optimization/continuity-ledger.md` | Cross-phase continuity, code summary writeback, and interface boundary ledger. |
| `docs/open_source_platform_optimization/next-window-prompt.md` | Copy-ready prompt for starting the next agent window. |

## Source Packet

- `source-packet.md` records current repository facts, open-source gaps, validation surfaces, release blockers, and phase ownership.
- `optimization-plan.md` is the human-readable plan summary.
- No external PRD, Figma file, dashboard, or credential has been imported.

## Current System Shape

- Root docs exist: `README.md`, `DEPLOY.md`, and `CHANGELOG.md`.
- Root package metadata in `pyproject.toml` declares MIT but the root `LICENSE` file is missing.
- GitHub workflows exist for CI, Docker publish, and SDK publish; CI coverage still needs alignment with frontend, compose, and harness gates.
- GAA-04 release readiness remains blocked by incomplete release env values, provider/model alignment, live seeded dynamic-route smoke, and optional topology evidence.
- `docs/open_source_platform_optimization/**` is now unignored so this plan can be committed.

## Assumptions and Decisions

- OSP-00 baseline planning is complete and unlocks OSP-01.
- Implementation phases must keep GAA-04 release blockers visible until fixed or explicitly waived.
- A phase that cannot identify concrete code boundaries must stop and record a blocker instead of guessing.

## Phase Order

| Phase | Name | Core Outcome | Report |
| --- | --- | --- | --- |
| Phase 00 | Open Source Baseline Audit | Current repo facts, blockers, phase boundaries, and validation surfaces are recorded. | `reports/osp-00-open-source-baseline-audit-report.md` |
| Phase 01 | Governance Legal And Security Trust | Root legal, governance, security, support, contribution, and project URL files are complete. | `reports/osp-01-governance-legal-and-security-trust-report.md` |
| Phase 02 | Contributor Experience And CI | Local contributor commands and CI gates prove backend, frontend, compose, and harness checks. | `reports/osp-02-contributor-experience-and-ci-report.md` |
| Phase 03 | Demo Data Documentation And Developer Experience | Demo seed path and docs prove core product journeys without private data. | `reports/osp-03-demo-data-documentation-and-developer-experience-report.md` |
| Phase 04 | Release Distribution And Community Readiness | Public release flow is reproducible, auditable, rollback-ready, and tied to GAA-04 blockers. | `reports/osp-04-release-distribution-and-community-readiness-report.md` |

## Roadmap Cohesion

The phase chain is `OSP-00 Open Source Baseline Audit
  -> OSP-01 Governance Legal And Security Trust
  -> OSP-02 Contributor Experience And CI
  -> OSP-03 Demo Data Documentation And Developer Experience
  -> OSP-04 Release Distribution And Community Readiness`. Each phase must inherit prior report evidence, preserve code/interface boundaries in the continuity ledger, and update the next handoff before unlocking dependent work. The terminal phase or release gate must run whole-demand regression across completed feature-oracle items before the full requirement is considered done.

## Delivery Quality Gates

- Every phase must be executable and verifiable on its own.
- Every phase must inherit prior evidence and record what it unlocks next.
- Every implementation must include test evidence and review evidence or an explicit blocker.
- The terminal phase or release gate must run whole-demand regression across completed feature-oracle items.
- Runtime files must be current enough for a fresh agent to resume after context compaction.

## New Window Prompt

Use `docs/open_source_platform_optimization/next-window-prompt.md` when starting a new Codex, Claude Code, or compatible agent window. Prefer the exact target phase `GOAL_PROMPT` from the phase file when assigning implementation work.

## Shared Harness Rules

- Stay inside phase boundaries.
- Make the smallest requirement-satisfying change.
- Plan before editing.
- Do not claim completion without durable test, review, and regression evidence.
- Document blockers and user waivers explicitly.
- Keep runtime files current enough for recovery after context compaction.

## Global Non-Goals

- Do not deploy.
- Do not mutate production data.
- Do not expand beyond the named phase chain without updating the manifest and continuity ledger.

## Global Compliance Gates

- Do not expose secrets.
- Do not perform destructive commands.
- Document approval before external service, migration, deployment, or production data changes.

## Standard Verification Commands

Starter validation discovery command:

```bash
rg --files -g 'package.json' -g 'pnpm-lock.yaml' -g 'yarn.lock' -g 'package-lock.json' -g 'pyproject.toml' -g 'requirements*.txt' -g 'pytest.ini' -g 'tox.ini' -g 'manage.py' -g 'pom.xml' -g 'build.gradle*' -g 'go.mod' -g 'Cargo.toml' -g 'pubspec.yaml' -g 'Makefile' -g '.github/workflows/*' .
```

This command is not completion evidence by itself. The baseline phase must inspect the listed project manifests, package scripts, build files, or CI workflows, then write the exact test, lint, typecheck, build, browser, eval, or smoke commands back into `source-packet.md`, `continuity-ledger.md`, and the phase report.

## Required Browser or Runtime Checks

- OSP-03 must include browser or API smoke evidence for seeded dynamic routes: knowledge detail, exam detail, public share, and public quiz.
- OSP-04 must include release runtime checks or an explicit blocker for missing release env, provider/model alignment, seed data, or topology evidence.

## External Inputs and Approvals

- No external inputs are captured by the starter scaffold.
- Any credential, dashboard, Figma file, deployment target, DNS/provider change, or migration approval must be added to the source packet before use.
