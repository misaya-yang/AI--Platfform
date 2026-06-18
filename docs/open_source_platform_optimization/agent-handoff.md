# Open Source Platform Optimization Harness Agent Handoff

**Created:** 2026-06-18

**Harness Folder:** `docs/open_source_platform_optimization`

---

## Planner Notes

- Source packet: `docs/open_source_platform_optimization/source-packet.md`
- Feature oracle: `docs/open_source_platform_optimization/feature-oracle.json`
- Continuity ledger: `docs/open_source_platform_optimization/continuity-ledger.md`
- Completed phases: `OSP-00` through `OSP-04`
- Next target phase: terminal verification, commit, and push
- Next target phase file: none
- Next feature-oracle item: terminal regression across `OSP-F001` through `OSP-F005`
- Baseline evidence: `docs/open_source_platform_optimization/optimization-plan.md`, `docs/open_source_platform_optimization/source-packet.md`, and `docs/open_source_platform_optimization/reports/osp-00-open-source-baseline-audit-report.md`.

## Generator Notes

- Work on one phase and one feature-oracle item at a time.
- Stay inside the phase `LIKELY_EDIT_PATHS`.
- Make the smallest requirement-satisfying change and justify any scope expansion.
- Summarize inspected code facts into `docs/open_source_platform_optimization/source-packet.md`.
- Update `docs/open_source_platform_optimization/progress-log.md`, `docs/open_source_platform_optimization/continuity-ledger.md`, and the phase report before handoff.
- Record test evidence and review evidence before marking a phase passed.
- OSP implementation is repository-only and complete pending final verification.
- Do not publish Docker images, SDKs, GitHub releases, or deploy environments without explicit owner approval.
- Do not hide GAA-04 release blockers while improving open-source posture.

## Evaluator Notes

- Read the phase report, changed files, validation output, and oracle evidence.
- Reject `passing` status when evidence is missing, superficial, outside the target phase, unreviewed, or broader than the minimal required change.
- Confirm terminal whole-demand regression before the full requirement is considered complete.
- Write findings as actionable file/line or command/check notes.

## Next Handoff

- Active role: generator/evaluator
- Active phase: terminal verification
- Active feature-oracle item: OSP-F001 through OSP-F005
- Required evidence before exit: strict OSP and GAA harness validation, JSON validation, compose config, focused Python lint/test, frontend type/lint/build, open-source Playwright route smoke, demo seed dry-run, git diff check, no-secret review, then commit and push if all required repository-only checks pass.
