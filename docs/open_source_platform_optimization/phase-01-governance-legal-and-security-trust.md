# Phase 01 - Governance Legal And Security Trust

> For agentic workers: enter plan-first mode before editing. Execute this phase only, make the smallest requirement-satisfying change, write the required evidence, and do not advance to the next phase until acceptance gates pass or blockers are documented.

**Goal:** Add the public trust surface required for an open-source platform while preserving prior release-safety and no-secret contracts.

**Architecture:** This phase inherits code facts from `source-packet.md`, boundary decisions from `continuity-ledger.md`, and prior evidence from the dependency phase report.

**Tech Stack:** Python/FastAPI gateway, assistant and knowledge microservices, React/Vite frontend, Docker Compose, GitHub Actions, SDK packages, and PRD harness docs.

---

## Machine Contract

The JSON block below is the authoritative machine-readable contract for goal-mode agents and validators. Keep it synchronized with the human-readable sections.

```json
{
  "schema_version": "prd-phase-harness/v3",
  "harness_role": "execution",
  "phase": {
    "id": "OSP-01",
    "number": "01",
    "title": "Governance Legal And Security Trust",
    "status": "passed",
    "type": "implementation",
    "repo_path": ".",
    "docs_path": "docs/open_source_platform_optimization",
    "phase_file": "docs/open_source_platform_optimization/phase-01-governance-legal-and-security-trust.md",
    "depends_on": [
      "OSP-00"
    ],
    "unlocks": [
      "OSP-02"
    ]
  },
  "goal": {
    "target": "Add the public trust surface required for an open-source platform while preserving prior release-safety and no-secret contracts.",
    "prompt": "Complete OSP-01 Governance Legal And Security Trust for `.` by following `docs/open_source_platform_optimization/phase-01-governance-legal-and-security-trust.md`; work on OSP-F002; add or repair root LICENSE, CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, support policy, issue and pull-request templates, and project URL metadata; preserve GAA release blockers and no-secret contracts; write code facts back to the source packet and continuity ledger; finish only after validation, regression, review, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.",
    "plan_required": true,
    "plan_output": "docs/open_source_platform_optimization/reports/osp-01-governance-legal-and-security-trust-plan.md",
    "completion_report": "docs/open_source_platform_optimization/reports/osp-01-governance-legal-and-security-trust-report.md"
  },
  "runtime": {
    "feature_oracle": "docs/open_source_platform_optimization/feature-oracle.json",
    "loop_contract": "docs/open_source_platform_optimization/loop-contract.json",
    "loop_state": "docs/open_source_platform_optimization/loop-state.json",
    "progress_log": "docs/open_source_platform_optimization/progress-log.md",
    "handoff": "docs/open_source_platform_optimization/agent-handoff.md",
    "continuity_ledger": "docs/open_source_platform_optimization/continuity-ledger.md",
    "next_window_prompt": "docs/open_source_platform_optimization/next-window-prompt.md",
    "session_boot": {
      "read_progress": true,
      "run_baseline_check": true,
      "update_progress_before_exit": true
    },
    "agent_roles": [
      "planner",
      "generator",
      "evaluator"
    ]
  },
  "context": {
    "read_first": [
      "docs/open_source_platform_optimization/README.md",
      "docs/open_source_platform_optimization/phase-manifest.md",
      "docs/open_source_platform_optimization/source-packet.md",
      "docs/open_source_platform_optimization/loop-contract.json",
      "docs/open_source_platform_optimization/loop-state.json",
      "docs/open_source_platform_optimization/feature-oracle.json",
      "docs/open_source_platform_optimization/progress-log.md",
      "docs/open_source_platform_optimization/agent-handoff.md",
      "docs/open_source_platform_optimization/continuity-ledger.md",
      "docs/open_source_platform_optimization/next-window-prompt.md",
      "docs/open_source_platform_optimization/phase-01-governance-legal-and-security-trust.md"
    ],
    "primary_context": [
      "docs/open_source_platform_optimization/README.md",
      "docs/open_source_platform_optimization/source-packet.md",
      "docs/open_source_platform_optimization/continuity-ledger.md",
      "docs/open_source_platform_optimization/reports/osp-00-open-source-baseline-audit-report.md",
      "pyproject.toml",
      ".github/workflows/ci.yml",
      ".github/workflows/docker-publish.yml",
      ".github/workflows/publish-sdk.yml",
      "README.md",
      "DEPLOY.md",
      "CHANGELOG.md"
    ],
    "context_budget": "focused",
    "do_not_load_unless": [
      "external dashboards",
      "production environments",
      "unrelated modules not named by the phase contract"
    ]
  },
  "boundaries": {
    "likely_edit_paths": [
      "LICENSE",
      "CONTRIBUTING.md",
      "SECURITY.md",
      "CODE_OF_CONDUCT.md",
      "SUPPORT.md",
      ".github/ISSUE_TEMPLATE/**",
      ".github/PULL_REQUEST_TEMPLATE.md",
      "pyproject.toml",
      "README.md",
      "DEPLOY.md",
      "docs/open_source_platform_optimization/source-packet.md",
      "docs/open_source_platform_optimization/continuity-ledger.md",
      "docs/open_source_platform_optimization/progress-log.md",
      "docs/open_source_platform_optimization/agent-handoff.md"
    ],
    "do_not_edit": [
      "production systems",
      "secret files",
      "runtime service configuration",
      "unrelated roadmap or product scopes"
    ],
    "external_inputs": [
      "none captured by scaffold; record any required external input before use"
    ],
    "secrets_required": []
  },
  "tool_policy": {
    "allowed_tools": [
      "repo search",
      "shell validation"
    ],
    "approval_required": [
      "production data mutation",
      "destructive commands",
      "external service changes",
      "deployment"
    ],
    "dangerous_commands": [
      "git reset --hard",
      "rm -rf",
      "production migration"
    ]
  },
  "risk": {
    "tags": [
      "security",
      "license",
      "community",
      "documentation"
    ],
    "data_mutation": "no",
    "migration_required": "no",
    "browser_required": "no",
    "ai_eval_required": "no",
    "external_service_required": "no",
    "release_blocking": "yes"
  },
  "validation": {
    "commands": [
      {
        "id": "governance-file-scan",
        "cwd": ".",
        "command": "test -f LICENSE && test -f CONTRIBUTING.md && test -f SECURITY.md && test -f CODE_OF_CONDUCT.md && test -f SUPPORT.md && test -f .github/PULL_REQUEST_TEMPLATE.md",
        "expected": "Required root governance and collaboration files exist.",
        "required": true
      },
      {
        "id": "project-url-review",
        "cwd": ".",
        "command": "python3 - <<'PY'\nfrom pathlib import Path\ntext = Path('pyproject.toml').read_text()\nassert 'github.com/your-org/ai-gateway' not in text\nPY",
        "expected": "Root package metadata no longer points at placeholder repository URLs.",
        "required": true
      },
      {
        "id": "harness-validation",
        "cwd": ".",
        "command": "python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/open_source_platform_optimization --strict --quality-score",
        "expected": "The OSP harness remains structurally valid after governance updates.",
        "required": true
      }
    ],
    "browser_checks": [
      "not required for OSP-01 because this phase edits repository trust and contribution files only"
    ],
    "regression_scope": [
      "prior phase report evidence and feature-oracle status remain valid"
    ],
    "compliance_gates": [
      "do not read or write secrets",
      "do not mutate production data",
      "do not weaken GAA release blockers or no-secret-output guarantees",
      "preserve bundled third-party license notices",
      "document approval before external service or deployment changes"
    ],
    "acceptance_gates": [
      "phase report exists with validation or blocker evidence",
      "feature-oracle item is updated with evidence or blocker notes",
      "source-packet and continuity-ledger code summaries are updated",
      "progress-log and agent-handoff name the next concrete action",
      "changed files stay inside the smallest phase edit boundary; any expansion is justified in the phase report",
      "self-review or evaluator review checks requirement coverage, test evidence, regression impact, and minimal-change scope",
      "runtime files contain enough current facts, blockers, decisions, and next actions for a fresh window after context compaction"
    ],
    "rollback_plan": [
      "revert phase-scoped changes and restore runtime docs from git if validation fails"
    ]
  },
  "evidence": {
    "outputs": [
      "docs/open_source_platform_optimization/reports/osp-01-governance-legal-and-security-trust-report.md"
    ],
    "required_artifacts": [
      "phase report",
      "progress-log entry",
      "feature-oracle evidence",
      "continuity-ledger update",
      "source-packet code summary",
      "handoff update",
      "review evidence",
      "minimal-change scope note"
    ],
    "waiver_policy": "Only mark a gate waived when the user explicitly waives it or the report documents a blocker and remaining evidence.",
    "next_phase_handoff": "State whether dependent phases are unlocked and what the next agent must know."
  },
  "stop_conditions": [
    "license choice or security contact ownership is unclear",
    "credentials or approvals are required but not documented",
    "destructive commands, production data access, or out-of-scope edits are required"
  ]
}
```

## Coding Agent Contract

- PHASE_ID: OSP-01
- GOAL_TARGET: Add the public trust surface required for an open-source platform while preserving prior release-safety and no-secret contracts.
- GOAL_PROMPT: Complete OSP-01 Governance Legal And Security Trust for `.` by following `docs/open_source_platform_optimization/phase-01-governance-legal-and-security-trust.md`; work on feature-oracle item OSP-F002; preserve dependency continuity with OSP-00; add or repair root LICENSE, CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, SUPPORT.md, issue and pull-request templates, and project URL metadata; write code facts and boundary decisions back before handoff; finish only after validation, regression, review, compliance, rollback, evidence, and acceptance gates pass or blockers are documented.
- DEPENDS_ON: OSP-00
- READ_FIRST: `docs/open_source_platform_optimization/README.md`, `docs/open_source_platform_optimization/phase-manifest.md`, this file
- PRIMARY_CONTEXT: docs/open_source_platform_optimization/README.md, docs/open_source_platform_optimization/source-packet.md, docs/open_source_platform_optimization/continuity-ledger.md, docs/open_source_platform_optimization/reports/osp-00-open-source-baseline-audit-report.md, pyproject.toml, .github/workflows/ci.yml, .github/workflows/docker-publish.yml, .github/workflows/publish-sdk.yml, README.md, DEPLOY.md, CHANGELOG.md
- LIKELY_EDIT_PATHS: LICENSE, CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, SUPPORT.md, .github/ISSUE_TEMPLATE/**, .github/PULL_REQUEST_TEMPLATE.md, pyproject.toml, README.md, DEPLOY.md, docs/open_source_platform_optimization/source-packet.md, docs/open_source_platform_optimization/continuity-ledger.md, docs/open_source_platform_optimization/progress-log.md, docs/open_source_platform_optimization/agent-handoff.md
- DO_NOT_EDIT: production systems, secret files, runtime service configuration, unrelated roadmap or product scopes
- EXECUTION_MODE: plan-first; implement stepwise; verify before completion; write evidence before handoff
- VALIDATION_COMMANDS: test -f LICENSE && test -f CONTRIBUTING.md && test -f SECURITY.md && test -f CODE_OF_CONDUCT.md && test -f SUPPORT.md && test -f .github/PULL_REQUEST_TEMPLATE.md; python3 -c "from pathlib import Path; text = Path('pyproject.toml').read_text(); assert 'github.com/your-org/ai-gateway' not in text"; python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/open_source_platform_optimization --strict --quality-score
- BROWSER_CHECKS: Not required for OSP-01 because this phase edits repository trust and contribution files only.
- REGRESSION_SCOPE: Prior phase report evidence, feature-oracle status, and continuity-ledger boundaries remain valid.
- COMPLIANCE_GATES: Do not read/write secrets, mutate production data, deploy, change external services, remove bundled third-party license notices, or weaken GAA release blockers without documented approval.
- ROLLBACK_PLAN: Revert phase-scoped changes and restore runtime docs from git if validation fails.
- ACCEPTANCE_GATES: Phase report exists; validation or blocker evidence is recorded; oracle item, progress log, handoff, source packet, and continuity ledger are updated; minimal-change scope and review evidence are recorded.
- EVIDENCE_OUTPUT: `docs/open_source_platform_optimization/reports/osp-01-governance-legal-and-security-trust-report.md`
- STOP_CONDITIONS: Stop if license choice, security contact ownership, credentials, approvals, destructive commands, production data access, or out-of-scope edits are required but undocumented.

## Harness Runtime

- FEATURE_ORACLE: `docs/open_source_platform_optimization/feature-oracle.json`
- LOOP_CONTRACT: `docs/open_source_platform_optimization/loop-contract.json`
- LOOP_STATE: `docs/open_source_platform_optimization/loop-state.json`
- PROGRESS_LOG: `docs/open_source_platform_optimization/progress-log.md`
- AGENT_HANDOFF: `docs/open_source_platform_optimization/agent-handoff.md`
- NEXT_WINDOW_PROMPT: `docs/open_source_platform_optimization/next-window-prompt.md`
- CONTINUITY_LEDGER: `docs/open_source_platform_optimization/continuity-ledger.md`

Session boot:

1. Read the runtime artifacts above.
2. Follow the loop contract: observe, select, execute, verify, record, decide.
3. Run the target phase's baseline or smoke validation before implementation when available.
4. Select one matching feature-oracle item and keep work scoped to that item and this phase.
5. Summarize inspected code facts and interface decisions back into the source packet and continuity ledger.
6. Record minimal-change scope and review/test evidence.
7. Update loop state, progress, continuity, and handoff files before exiting.

## Feature Oracle Policy

The feature oracle is the durable test list for long-running agents. Do not delete oracle cases to make completion easier. Update only `status`, `evidence`, and `notes` unless the user explicitly changes scope.

Status rules:

- `failing`: not implemented or not verified.
- `passing`: end-to-end evidence exists.
- `blocked`: a named dependency, credential, environment, or scope issue prevents completion.
- `waived`: the user explicitly waived the case and remaining risk is documented.

## Task Spec

Execute OSP-01 by using the phase contract, updating OSP-F002, and preserving the dependency chain `OSP-00 Open Source Baseline Audit
  -> OSP-01 Governance Legal And Security Trust
  -> OSP-02 Contributor Experience And CI
  -> OSP-03 Demo Data Documentation And Developer Experience
  -> OSP-04 Release Distribution And Community Readiness`.

## Cross-Phase Continuity

- Depends on: OSP-00
- Unlocks: OSP-02
- Feature-oracle item: OSP-F002
- Continuity ledger: `docs/open_source_platform_optimization/continuity-ledger.md`
- Prior-phase evidence to inherit: the OSP-00 phase report, progress-log entry, oracle evidence, and continuity-ledger boundary notes
- Boundary this phase must preserve for later phases: code/interface facts written by prior phases and any downstream contract named in the continuity ledger
- Handoff this phase must produce: phase report, progress-log entry, oracle evidence, source-packet code summary, continuity-ledger update, and agent-handoff next action

## Code Summary Writeback

Before claiming completion, inspect the code paths allowed by this phase and write back:

- `source-packet.md`: summarize discovered files, services, routes, schemas, tests, commands, and runtime constraints.
- `continuity-ledger.md`: record interface boundaries, dependency assumptions, changed contracts, and any downstream phase impact.
- `agent-handoff.md`: state the next concrete action, active feature-oracle item, validation evidence, and blocker status.
- Phase report: link validation output, review evidence, minimal-change scope notes, and the exact code-summary update.

## Problem Boundary

In scope:

- Work needed to satisfy OSP-F002 for Governance Legal And Security Trust.
- Code inspection and summary writeback required to keep later phases aligned.

Out of scope:

- Production deployment.
- Production data mutation.
- Unrelated feature work outside this phase chain.

## Context Policy

Before editing, inspect:

- Start with this phase file, `source-packet.md`, `continuity-ledger.md`, and the OSP-00 phase report.
- Confirm inherited code facts and boundaries before editing.
- Expand repo context only to paths you record back into the source packet.

Do not load unrelated files unless a blocker requires expanding context.

## Requirements

### R1 Root Trust Files

The repository must expose root `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and `SUPPORT.md` files with public, contributor-facing content.

### R2 Collaboration Templates

GitHub issue and pull-request templates must exist in `.github` and must ask for reproduction, validation, security sensitivity, and release impact without requesting secret values.

### R3 Package Metadata

Root project URLs in `pyproject.toml` must point at the actual public repository or be documented as intentionally pending. Placeholder `your-org` URLs must not remain.

### R4 Continuity-Preserving Execution

OSP-01 must update OSP-F002, produce durable evidence, and write code/interface facts back so the next phase can continue without hidden chat context.

## Test and Regression Requirements

Run the root governance file check, project URL review, strict OSP harness validator, `git diff --check`, and a no-secret filename/content review over the newly added governance/template files. If project ownership or security contact details are unknown, stop and record a blocker instead of inventing maintainer policy.

## Compliance and Safety Requirements

Do not expose secrets, mutate production data, deploy, use external services, remove bundled third-party license notices, or weaken GAA release blockers unless the required approval and source packet entry exist.

## Rollback and Recovery

Revert phase-scoped code changes, restore runtime docs from git, and mark the oracle item blocked if validation cannot be recovered.

## Execution Capture

Write the phase report, append progress-log evidence, update oracle evidence, update continuity-ledger boundaries, and refresh agent-handoff next action.

Use ``docs/open_source_platform_optimization/reports/phase-report-template.md`` when writing the phase report.

## Evaluator Protocol

Reject completion if evidence is missing, tests or review are absent, code facts were not written back, continuity boundaries are stale, scope expansion lacks justification, or the phase tries to unlock dependent work without a report.

## Acceptance Criteria

- OSP-F002 has evidence or a documented blocker.
- Root trust files and GitHub templates exist or ownership blockers are documented.
- Package URL placeholders are removed or explicitly blocked with owner action.
- `docs/open_source_platform_optimization/reports/osp-01-governance-legal-and-security-trust-report.md` exists or is named as blocked evidence.
- `source-packet.md`, `continuity-ledger.md`, `progress-log.md`, and `agent-handoff.md` reflect the latest code facts and next action.
- Minimal-change scope and review/test evidence are recorded.

## Risks

- Phase isolation can break if downstream boundary changes are not recorded.
- Implementation can drift if code summaries stay stale.
- Long-running agents can repeat work if handoff evidence is incomplete.
