# GAA-04 Deployment Readiness Plan

**Phase:** GAA-04 Deployment Readiness

**Feature:** GAA-F005

**Date:** 2026-06-17

## Selected Scope

Run the non-destructive release-readiness gates available in this workspace and document blocked gates honestly. Do not deploy, migrate production data, rotate credentials, mutate provider dashboards, or delete Docker volumes.

## Planned Checks

| Gate | Command or Check | Expected Handling |
| --- | --- | --- |
| Compose static config | `docker compose --env-file .env.example config --quiet` | Must pass without real secrets. |
| Config validation | `make validate-config` | Expected to block when `.env` is absent. |
| Runtime validation | `make validate` | Expected to block when `.env` is absent. |
| Runtime status | `make status` and `docker compose ps` | Summarize health without printing secret values. |
| First-run migration usability | Review and test `make quickstart` sequencing | Quickstart should wait for PostgreSQL, run idempotent migrations with the selected env file, then run runtime validation. |
| Base schema initialization | Review and test `migrate.sh --auto` on an empty base schema | Automatic migration should apply `database/schema.sql` before numbered migrations when the base schema is missing. |
| Service isolation | `make test-isolation` | Run non-destructive integration gates; record skips if live dependencies are unavailable. |
| Browser smoke | `E2E_REUSE_SERVER=1 ... playwright test web/e2e/site-walkthrough.spec.ts` | Reuse the running local stack and record result. |
| Dynamic route seed probe | Read-only probes for knowledge datasets, exams, quizzes, and conversation shares | Add discovered detail/share/quiz routes to browser smoke when records exist; record zero-record state as a release blocker, not a pass. |
| Mocked dynamic route render smoke | `E2E_REUSE_SERVER=1 ... playwright test web/e2e/dynamic-route-render.spec.ts` | Verify dynamic page renderability without database writes; do not treat it as live seeded-record coverage. |
| Assistant eval inheritance | GAA-03 report and targeted eval commands | Confirm release gate inherits passing eval/safety evidence. |
| Rollback readiness | Read `Makefile`, `DEPLOY.md`, and scripts | Document non-destructive rollback commands and approval gates. |
| Rollback script contract | Review `scripts/new/backup.sh`, `scripts/new/migrate.sh`, and related script tests | Backup/restore must use the selected env file and fail on SQL restore errors instead of reporting success; backup/migrate typoed or incomplete arguments must fail before action. |

## Likely Blockers

- `.env` is absent in the repository root, so `make validate-config`, `make validate`, deployment, and provider-backed checks cannot be honestly marked passed.
- Real chat and embedding provider keys are external inputs and must not be inferred from container env or printed.
- Deployment requires explicit user approval and is out of scope for this run.

## Evidence Output

Write `docs/general_ai_assistant_upgrade/reports/gaa-04-deployment-readiness-report.md` with pass/block status, command evidence, rollback plan, and final launch decision.
