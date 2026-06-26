# NGA-05 / NGA-F012 Plan

## Scope

- Active phase: `NGA-05 Evaluation Safety and Release Gate`
- Active feature: `NGA-F012`
- Allowed implementation paths: assistant safety/integration tests, frontend smoke specs, README/RELEASE notes, and `docs/general_ai_assistant_next_gen/**`
- Expected first approach: validation and evidence writeback. Code changes only if a required gate exposes a narrow, in-scope defect.

## Observation

- NGA-04 is passed and unlocks the terminal release gate.
- The phase requires whole-demand regression across `NGA-F001` through `NGA-F012`.
- Primary safety context already contains deterministic tests for memory prompt-boundary sanitization, PII redaction, guardrails, SSRF-safe fetch, and safe-fetch callsites.
- External env validation is allowed only by path and must not print secret values.

## Plan

1. Run the required assistant safety pytest command.
2. Run the required assistant integration pytest command.
3. Run frontend release checks in the phase command order, recording exact failures or environment blockers.
4. Run Docker Compose static config validation against `.env.example`.
5. Inspect and run the Makefile env validation gates without printing secret values; if they require unavailable external state, record a precise blocker by command and variable/service name only.
6. Build a whole-demand regression table for every feature-oracle item from `NGA-F001` through `NGA-F012`.
7. Write the NGA-05 actor report, independent critic artifact, source-packet addendum, continuity ledger, progress log, handoff, loop-state, next-window prompt, and only F012 status/evidence/notes.
8. Run JSON checks, `git diff --check`, strict harness validation, and terminal completion gate when the report state is final.

## Constraints

- No deployment, package publishing, credential rotation, migration, production data access, destructive git operations, or secret printing.
- Keep release blockers separate from code-delivery status.
- If a gate cannot run safely here, classify F012 as blocked rather than inventing evidence.
