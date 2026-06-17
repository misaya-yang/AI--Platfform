# GAA-00 Baseline Release Audit Report

**Date:** 2026-06-16

**Phase:** GAA-00

**Feature:** GAA-F001

**Status:** passing with runtime blockers recorded

## Summary

Baseline inspection and static/local validation are complete. The repo compiles and the Python suite passes with dev/test extras installed. Full authenticated page walkthrough and live service-failure isolation remain blocked by missing `.env` and absent compose runtime.

## Files Inspected

- `README.md`
- `DEPLOY.md`
- `Makefile`
- `docker-compose.yml`
- `docker-compose.dev.yml`
- `pyproject.toml`
- `web/package.json`
- `web/src/router.tsx`
- `web/e2e/site-walkthrough.spec.ts`
- `src/api/v1/assistant.py`
- `packages/ai-gateway-core/src/ai_gateway_core/proxy/drain.py`
- `tests/api/test_assistant_sessions.py`
- `tests/proxy/test_drain.py`

## Validation Evidence

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov` | Passed: 2535 passed, 44 skipped, 83 warnings. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/api/test_assistant_sessions.py tests/proxy/test_drain.py` | Passed: 19 passed, 1 skipped, 1 warning. |
| `uv run ruff check src/api/v1/assistant.py packages/ai-gateway-core/src/ai_gateway_core/proxy/drain.py tests/api/test_assistant_sessions.py tests/proxy/test_drain.py` | Passed. |
| `pnpm -C web type-check` | Passed. |
| `pnpm -C web build` | Passed; Vite emitted large chunk warnings. |
| `pnpm -C web lint` | Passed with 0 errors and 39 warnings. |
| `docker compose --env-file .env.example config --quiet` | Passed. |
| `docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file .env.example config --quiet` | Passed. |
| `git diff --check` | Passed. |

## Blocked Runtime Checks

| Check | Blocker | Required Next Action |
| --- | --- | --- |
| `bash scripts/new/validate-env.sh --config-only` | `.env` file is absent. | Create `.env` from `.env.example`, fill generated secrets and provider keys, then rerun. |
| `make validate` | compose services are not running and `.env` is absent. | Start stack after `.env` is configured. |
| `pnpm -C web e2e` | E2E stack and user are not available. | Run after compose stack is healthy and E2E user exists. |
| live service-failure isolation | compose services are not running. | Run after `docker compose up -d --wait`. |

## Code Review Findings

- No critical issue found in the scoped diff after targeted tests.
- `src/api/v1/assistant.py` now classifies missing artifact schema errors without direct API-layer `asyncpg` import names.
- `web/docker-entrypoint.d/40-runtime-config.sh` now normalizes CR/LF in runtime env values before writing JS.
- Frontend lint warnings remain, mostly React hook dependency warnings. They are not new build blockers but should be addressed before a strict UI quality gate.

## Deployment Readiness

Static compose config passes for production and dev overlay. Runtime deploy is not verified because `.env` is absent and no compose stack is running.

## Oracle Update

`GAA-F001` is marked `passing` because command evidence and this report exist. The notes record runtime blockers.

## Unlock Decision

GAA-01 is unlocked for assistant core contract work. GAA-02 and GAA-04 still require runtime/browser evidence before they can pass.
