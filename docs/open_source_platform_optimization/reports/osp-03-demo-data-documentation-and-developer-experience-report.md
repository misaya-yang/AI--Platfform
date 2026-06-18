# OSP-03 Demo Data, Documentation, And Developer Experience Report

**Date:** 2026-06-18

## Result

Passed.

## Changes

- Added deterministic local demo SQL at `examples/demo-data/open-source-demo.sql`.
- Added dry-run/apply wrapper `scripts/new/seed-demo-data.sh`.
- Added `make seed-demo` and `make seed-demo-apply`.
- Added `docs/demo-data.md` and README demo-data entry points.
- Aligned `web/e2e/dynamic-route-render.spec.ts` with the seeded demo IDs.
- Added `web/playwright.opensource.config.ts`.
- Added `web/package.json` script `e2e:opensource`.

## Validation Evidence

Commands run locally:

```bash
scripts/new/seed-demo-data.sh --dry-run
uv run --extra dev --extra test pytest -q --no-cov tests/scripts/test_seed_demo_data.py
pnpm -C web e2e:opensource
```

Observed results:

- Demo seed dry-run completed without requiring an env file or database connection.
- Demo seed tests passed: 2 tests passed.
- Open-source route smoke passed: 2 Playwright tests passed.

## Review Notes

The seed applies only when the user explicitly runs `--apply` or `make seed-demo-apply`. The default command is dry-run. No production data was read or written.

## Handoff

OSP-04 is unlocked. Release docs must tie CI, demo data, artifact workflows, rollback, and owner-only external gates together.
