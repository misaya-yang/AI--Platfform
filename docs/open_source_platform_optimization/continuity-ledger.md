# Open Source Platform Optimization Harness Continuity Ledger

**Created:** 2026-06-18

**Harness Folder:** `docs/open_source_platform_optimization`

---

## Purpose

This file preserves cross-phase continuity for long-running agents. Treat it as the bridge between product intent, code facts, execution evidence, and the next agent's starting point.

## Phase Continuity Chain

| Phase | Feature | Depends On | Unlocks | Handoff Boundary | Required Writeback |
| --- | --- | --- | --- | --- | --- |
| OSP-00 | OSP-F001 | none | OSP-01 | baseline report plus open-source readiness facts | source-packet and continuity-ledger code facts |
| OSP-01 | OSP-F002 | OSP-00 | OSP-02 | phase report plus handoff notes | source-packet and continuity-ledger code facts |
| OSP-02 | OSP-F003 | OSP-01 | OSP-03 | phase report plus handoff notes | source-packet and continuity-ledger code facts |
| OSP-03 | OSP-F004 | OSP-02 | OSP-04 | phase report plus handoff notes | source-packet and continuity-ledger code facts |
| OSP-04 | OSP-F005 | OSP-03 | none | phase report plus handoff notes | source-packet and continuity-ledger code facts |

## Interface Boundary Ledger

| Boundary | Current Fact | Source | Last Verified | Owner Phase |
| --- | --- | --- | --- | --- |
| Repository identity | Root package is `ai-gateway` version `2.0.0`; `pyproject.toml` declares MIT and project URLs under `https://github.com/misaya-yang/AI--Platfform`. | `pyproject.toml` | 2026-06-18 | OSP-01 |
| Runtime architecture | Gateway entrypoint is `src/main.py`; assistant runtime lives in `apps/assistant-service`; knowledge runtime lives in `apps/knowledge-service`; shared primitives live in `packages/ai-gateway-core`. | repo inspection, GAA source packet | 2026-06-18 | OSP-00 |
| Governance boundary | Root `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, PR template, and issue templates now define public project expectations. | root governance files, `.github` templates | 2026-06-18 | OSP-01 |
| CI boundary | GitHub CI now runs stable script contracts, focused Python tests, compose config, portable harness JSON checks, frontend type/lint/build, Playwright open-source route smoke, release docs, and demo seed dry-run. Repo-wide Ruff/format remains a historical debt and is intentionally not a required public gate yet. | `.github/workflows/ci.yml`, local validation | 2026-06-18 | OSP-02 |
| Demo-data boundary | Local demo data now uses deterministic SQL plus dry-run/apply wrapper and Make targets; frontend route smoke covers seeded knowledge, exam, share, and quiz IDs without a real backend. | `examples/demo-data/open-source-demo.sql`, `scripts/new/seed-demo-data.sh`, `web/e2e/dynamic-route-render.spec.ts` | 2026-06-18 | OSP-03 |
| Release boundary | Public release docs now cover local checks, tag workflows, post-release smoke, blockers, and rollback. Live production readiness still depends on owner env/provider/package approvals and GAA-04 evidence. | `RELEASE.md`, `CHANGELOG.md`, `docs/general_ai_assistant_upgrade/reports/gaa-04-deployment-readiness-report.md` | 2026-06-18 | OSP-04 |
| Edit boundary | OSP-01 may edit root governance files, `.github` templates, `pyproject.toml` URLs, README/DEPLOY links, and harness evidence files. Later phases must keep their edits inside their phase-specific files unless the report documents scope expansion. | `optimization-plan.md`, phase contracts | 2026-06-18 | OSP-00 |
| Handoff boundary | Do not unlock a dependent phase until report evidence, oracle evidence, progress log, and this ledger are updated. | phase report | 2026-06-18 | OSP-00 |

## Code Summary Writeback Rules

- After inspecting code, summarize discovered files, services, routes, schemas, tests, and runtime commands back into `source-packet.md`.
- Record cross-phase interface decisions here before handing off, especially API contracts, shared state, data shape, UI route assumptions, eval criteria, and rollback boundaries.
- If a phase changes a boundary another phase depends on, update that dependent phase's report handoff and the relevant oracle item notes.
- If a second agent cannot identify the next concrete action from this file, `progress-log.md`, and `agent-handoff.md`, stop and write a blocker instead of guessing.

## Current Continuity Status

- Completed phases: OSP-00 through OSP-04.
- Active phase: none.
- Active feature-oracle item: none.
- Current decision: public open-source readiness gaps identified by OSP-00 have been addressed with governance files, stable CI, demo data, mocked route smoke, and release documentation.
- Next action: after final verification, commit and push the open-source readiness changes. Production deployment and package publishing remain outside this harness without explicit owner approval.
