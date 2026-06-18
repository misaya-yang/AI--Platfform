# Open Source Platform Optimization Plan

**Date:** 2026-06-18

**Repository:** `/Users/misaya.yanghejazfs.com.au/misaya_project/AI--Platfform`

**Objective:** turn the current AI Gateway repository into a contributor-ready, release-ready open-source platform without weakening the existing deployment, security, assistant, knowledge-service, or runtime validation contracts.

## Current Evidence

The repository is already beyond a raw prototype:

- Root `README.md`, `DEPLOY.md`, `CHANGELOG.md`, `.env.example`, Docker Compose files, Makefile targets, SDK folders, and GAA release-readiness harness exist.
- GitHub Actions exist in `.github/workflows/ci.yml`, `.github/workflows/docker-publish.yml`, and `.github/workflows/publish-sdk.yml`; CI now includes stable script, compose, frontend, route-smoke, release-doc, and demo dry-run gates.
- The root Python package declares MIT in `pyproject.toml`, and the repository now has a root `LICENSE`.
- Root governance, contribution, security, support, issue, and PR template files are present.
- The frontend package is marked private in `web/package.json`; this is acceptable for a monorepo web app, but the public distribution boundary must be documented.
- GAA-04 release readiness is still the authority for owner-controlled external gates: real env values, provider/model alignment, registry credentials, deployment topology, and production launch approval.
- Demo data now exists for local developer proof: `examples/demo-data/open-source-demo.sql`, `scripts/new/seed-demo-data.sh`, `docs/demo-data.md`, and `pnpm -C web e2e:opensource`.
- `.gitignore` previously allowed only `docs/general_ai_assistant_upgrade/**`; this harness explicitly adds `docs/open_source_platform_optimization/**` as a tracked planning artifact.

## Non-Goals

- Do not deploy or publish packages from this plan.
- Do not rotate credentials, print secrets, or commit real `.env` files.
- Do not mutate production data.
- Do not remove release blockers by waiver unless the user explicitly approves the waiver and the report records residual risk.
- Do not replace the existing GAA harness; this plan depends on its release-readiness evidence.

## Target End State

The platform is repository-level open-source ready when all of these are true:

- A first-time contributor can clone the repo, understand the architecture, configure a local env, run checks, and open a useful demo without private context.
- Governance, legal, security, support, and contribution expectations are present at the repository root.
- CI proves the same core checks documented in README and release docs, including focused backend script lint/tests, frontend type/lint/build, compose static config, portable harness JSON checks, route smoke, and demo seed dry-run.
- Demo seed data and smoke tests cover the visible dynamic product journeys that do not require private provider keys: knowledge dataset detail, exam detail, public share, and public quiz routes.
- Release workflows have documented versioning, artifact, rollback, SBOM/provenance or explicit blocker, and tag-to-image/package traceability.
- GAA-04 external release blockers remain visible until fixed with evidence or explicitly waived with owner, reason, and residual risk.

## Phase Plan

| Phase | Purpose | Primary Deliverables | Completion Evidence |
| --- | --- | --- | --- |
| OSP-00 Open Source Baseline Audit | Freeze current repo facts and plan boundaries. | This plan, source packet, continuity ledger, baseline report, oracle update. | `osp-00-open-source-baseline-audit-report.md`, strict harness validation. |
| OSP-01 Governance Legal And Security Trust | Add public trust and collaboration files. | Root `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, support policy, issue and PR templates, corrected project URLs. | File existence checks, link checks where available, no secret output, package metadata review. |
| OSP-02 Contributor Experience And CI | Make contribution checks repeatable locally and in CI. | Documented `make` or script targets, CI matrix aligned with repo commands, frontend/backend/cache setup notes, CI badge or status reference. | GitHub workflow lint, backend test subset, frontend type/lint/build, compose config, harness validator. |
| OSP-03 Demo Data Documentation And Developer Experience | Give new users a working local product path. | Seed/demo script, demo data docs, screenshots or Playwright evidence, troubleshooting guide, architecture diagram or text map. | Seed dry-run or local-run evidence, browser smoke with seeded dynamic routes, no production data mutation. |
| OSP-04 Release Distribution And Community Readiness | Make public releases reproducible and auditable. | Versioning rules, release checklist, image/package publishing checks, rollback notes, GAA-04 blocker closure or waiver table. | Tag dry-run or workflow review, compose validation, release checklist report, whole-demand regression across OSP and GAA evidence. |

## Feature Oracle Summary

- `OSP-F001`: baseline plan and current open-source readiness facts are captured.
- `OSP-F002`: repository has complete governance, legal, security, support, and contribution surface.
- `OSP-F003`: local and CI checks are aligned, documented, and runnable by contributors.
- `OSP-F004`: demo data and docs prove core product flows without private data.
- `OSP-F005`: public release workflow is reproducible, auditable, and blocked only by explicit owner-approved waivers.

## Validation Strategy

Use these checks as the default evidence set. A phase may add more scoped checks, but it must not claim completion with weaker evidence.

```bash
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/open_source_platform_optimization --strict --quality-score
git diff --check
docker compose --env-file .env.example config --quiet
uv run ruff check tests/scripts/test_validate_env_quickstart.py tests/scripts/test_web_runtime_config_entrypoint.py tests/scripts/test_seed_demo_data.py
uv run --extra dev --extra test pytest -q --no-cov tests/scripts/test_web_runtime_config_entrypoint.py tests/scripts/test_validate_env_quickstart.py tests/scripts/test_seed_demo_data.py
pnpm -C web type-check
pnpm -C web lint
pnpm -C web build
pnpm -C web e2e:opensource
scripts/new/seed-demo-data.sh --dry-run
```

When a command cannot run in the current environment, the phase report must record the command, the exact blocker, and what a pass would prove.

## Risk Controls

- **Secrets:** only names of env variables may be recorded; values are never printed or committed.
- **Licensing:** third-party bundled license files under docgen and assistant skills must remain intact; root license choice must match package metadata.
- **Security:** public docs must not weaken HMAC, rate limit, auth domain, internal service, or no-secret-output guarantees.
- **Data:** demo seed work must use local/dev data only and must have a dry-run path before mutation.
- **Release:** publishing, production deployment, DNS/provider changes, and migration against shared data require explicit user approval.

## Execution Status

`OSP-00` through `OSP-04` are implemented at repository level. Final exit requires terminal verification, commit, and push.
