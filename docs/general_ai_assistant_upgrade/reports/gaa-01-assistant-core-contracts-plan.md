# GAA-01 Assistant Core Contracts Plan

**Date:** 2026-06-16

**Target Feature:** GAA-F002

## Selected Slice

Artifact schema-missing read contract for first-run or partially restored deployments.

## Plan

1. Confirm current assistant artifact endpoints and tests.
2. Extend missing schema handling from session artifact list to artifact metadata, download, and delete lookup paths.
3. Add focused tests that prove schema-missing lookup returns the public `404 Artifact not found` contract.
4. Run GAA-01 pytest targets, scoped ruff, and full pytest.
5. Write phase report and update oracle, source packet, continuity ledger, progress log, handoff, and next-window prompt.

## Scope Boundary

Allowed code edits: `src/api/v1/assistant.py` and `tests/api/test_assistant_sessions.py`.

Boundary expansion: `.gitignore` is edited because the harness path was ignored by the repo's blanket `docs/*` rule and would otherwise be difficult to hand off.
