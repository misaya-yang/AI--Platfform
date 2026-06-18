# OSP-02 Contributor Experience And CI Report

**Date:** 2026-06-18

## Result

Passed.

## Changes

- Reworked `.github/workflows/ci.yml` into portable jobs:
  - script contracts
  - Docker Compose and harness JSON checks
  - frontend typecheck, lint, build, and open-source route smoke
  - release-readiness documentation checks
- Kept CI independent from maintainer-local Codex skill paths.
- Added `tests/scripts/test_seed_demo_data.py` to prove demo seed dry-run behavior.

## Validation Evidence

Commands run locally:

```bash
bash -n scripts/new/validate-env.sh scripts/new/seed-demo-data.sh
uv run ruff check tests/scripts/test_validate_env_quickstart.py tests/scripts/test_web_runtime_config_entrypoint.py tests/scripts/test_seed_demo_data.py
uv run --extra dev --extra test pytest -q --no-cov tests/scripts/test_validate_env_quickstart.py tests/scripts/test_web_runtime_config_entrypoint.py tests/scripts/test_seed_demo_data.py
docker compose --env-file .env.example config --quiet
pnpm -C web type-check
pnpm -C web lint
pnpm -C web build
```

Observed results:

- Ruff focused checks passed.
- Focused pytest passed: 64 tests passed, 1 warning.
- Docker Compose static config passed.
- Frontend typecheck passed.
- Frontend lint passed with 0 errors and 39 existing warnings.
- Frontend build passed with Vite chunk-size warnings only.

## Review Notes

Repo-wide `ruff check src apps packages tests` and repo-wide format checks were intentionally not made mandatory because local reconnaissance found large pre-existing historical debt. CI now gates stable open-source onboarding and release-critical checks instead of hiding that debt behind a failing all-repo gate.

## Handoff

OSP-03 is unlocked. Demo data must provide a dry-run path and route smoke that does not require private data or live model keys.
