# General AI Assistant Upgrade Harness Phase Manifest

This is the compact index for coding agents. Use it with the target phase file.

## Grep Usage

```bash
rg -n "PHASE_ID: GAA-XX" docs/general_ai_assistant_upgrade
rg -n "GOAL_PROMPT:" docs/general_ai_assistant_upgrade
rg -n "VALIDATION_COMMANDS:" docs/general_ai_assistant_upgrade
rg -n "ACCEPTANCE_GATES:" docs/general_ai_assistant_upgrade
```

## Phase Index

| PHASE_ID | File | Depends On | Goal Target | Main Validation | Evidence Output |
| --- | --- | --- | --- | --- | --- |
| GAA-00 | `phase-00-baseline-release-audit.md` | none | Establish verified release and code baseline for the assistant upgrade. | full backend suite, frontend build/type/lint, compose static config | `reports/gaa-00-baseline-release-audit-report.md` |
| GAA-01 | `phase-01-assistant-core-contracts.md` | GAA-00 | Upgrade one assistant core contract slice while preserving gateway and service boundaries. | assistant API/runtime pytest targets plus ruff | `reports/gaa-01-assistant-core-contracts-report.md` |
| GAA-02 | `phase-02-assistant-user-experience.md` | GAA-01 | Upgrade one assistant UI slice with route and browser evidence. | frontend type/build/lint plus Playwright walkthrough | `reports/gaa-02-assistant-user-experience-report.md` |
| GAA-03 | `phase-03-ai-evaluation-and-safety.md` | GAA-02 | Add golden assistant behavior and safety evidence for the selected capability. | assistant golden, guardrail, safe-fetch, tool orchestration pytest targets | `reports/gaa-03-ai-evaluation-and-safety-report.md` |
| GAA-04 | `phase-04-deployment-readiness.md` | GAA-03 | Prove release readiness, runtime health, rollback, and monitoring gates. | make validation, compose runtime health, E2E smoke | `reports/gaa-04-deployment-readiness-report.md` |

## Phase Report Index

| PHASE_ID | Required Report |
| --- | --- |
| GAA-00 | `reports/gaa-00-baseline-release-audit-report.md` |
| GAA-01 | `reports/gaa-01-assistant-core-contracts-report.md` |
| GAA-02 | `reports/gaa-02-assistant-user-experience-report.md` |
| GAA-03 | `reports/gaa-03-ai-evaluation-and-safety-report.md` |
| GAA-04 | `reports/gaa-04-deployment-readiness-report.md` |

## Dependency Flow

```text
GAA-00 -> GAA-01 -> GAA-02 -> GAA-03 -> GAA-04
```

## Validation Matrix

| PHASE_ID | Mutates Data | Needs Browser/UI | Needs Agent/LLM Eval | Needs Migration | Needs External Service | Release Blocking |
| --- | --- | --- | --- | --- | --- | --- |
| GAA-00 | no | no | no | no | no | no |
| GAA-01 | no by default | no | yes | no | mockable | no |
| GAA-02 | no by default | yes | no | no | mockable | no |
| GAA-03 | no by default | no | yes | no | mockable | no |
| GAA-04 | no by default | yes | yes | possible after report approval | yes | yes |

## Risk Matrix

| PHASE_ID | Primary Risk | Stop Condition |
| --- | --- | --- |
| GAA-00 | false launch confidence from static-only checks | stop if baseline commands cannot be reproduced or runtime blockers are not recorded |
| GAA-01 | assistant contract drift across gateway, assistant-service, and shared core | stop if tenant isolation, artifact access, streaming, or service boundary cannot be tested |
| GAA-02 | page looks compiled but fails under authenticated runtime flow | stop if Playwright route walkthrough cannot run and no waiver exists |
| GAA-03 | non-deterministic AI behavior hides safety or tool-boundary regression | stop if golden eval evidence or mock provider path is missing |
| GAA-04 | deploy starts without secrets, rollback, or health evidence | stop if `.env`, Docker, health checks, or rollback proof is missing |

## Runtime Artifacts

| Artifact | Path | Agent Rule |
| --- | --- | --- |
| Loop Contract | `loop-contract.json` | Follow observe, select, execute, verify, record, decide. |
| Loop State | `loop-state.json` | Keep active phase, feature, iteration, status, and next action current. |
| Feature Oracle | `feature-oracle.json` | Update only status, evidence, and notes for the active feature. |
| Progress Log | `progress-log.md` | Append session start, validation, blocker, and exit notes. |
| Agent Handoff | `agent-handoff.md` | Keep planner, generator, and evaluator notes file-based. |
| Continuity Ledger | `continuity-ledger.md` | Preserve phase relatedness, code-summary writeback, and interface decisions. |
| Next Window Prompt | `next-window-prompt.md` | Use this to restart work in a fresh context window. |

## Agent Role Handoffs

- Planner: update contracts and oracle only when scope changes.
- Generator: execute exactly one phase and one oracle item.
- Evaluator: review changed files, command output, runtime evidence, and oracle status.

## Goal Setup Templates

```text
Complete GAA-01 Assistant Core Contracts for `.` by following `docs/general_ai_assistant_upgrade/phase-01-assistant-core-contracts.md`; work on feature-oracle item GAA-F002; stay inside named edit boundaries; finish only after validation, regression, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
```

## Shared Agent Rules

- Open only `READ_FIRST` and `PRIMARY_CONTEXT` before planning.
- Expand edit scope only after documenting a blocker.
- Write the phase report before moving to a dependent phase.
- Never mark an oracle item passing without evidence.

## External Inputs Checklist

- `.env` with generated local secrets and provider keys.
- Docker engine and Docker Compose.
- E2E user credentials for Playwright.
- Explicit approval for deployment, production migration, production data mutation, or credential rotation.
