# Open Source Platform Optimization Harness Next Window Prompt

Use this prompt to resume the final OSP-04 verification in a fresh Codex,
Claude Code, or Agent Skills-compatible window.

```text
Use $prd-phase-harness to continue the harness at `docs/open_source_platform_optimization`.

Target phase: OSP-04
Target phase file: `docs/open_source_platform_optimization/phase-04-release-distribution-and-community-readiness.md`
Target feature-oracle item: OSP-F005

Current state: OSP-00 through OSP-04 have repository-only implementation
complete. The repo now has root governance files, issue/PR templates, corrected
project URLs, stable open-source CI, deterministic demo data, a demo seed
dry-run/apply script, mocked frontend route smoke for seeded dynamic pages, and
a public release checklist. Treat this as the terminal release-readiness phase:
work on exactly one phase and one feature-oracle item, but include whole-demand
regression evidence across completed OSP items before commit.

Cold-start protocol:
1. Open `docs/open_source_platform_optimization/README.md`.
2. Open `docs/open_source_platform_optimization/phase-manifest.md`.
3. Open `docs/open_source_platform_optimization/loop-contract.json`.
4. Open `docs/open_source_platform_optimization/loop-state.json`.
5. Open `docs/open_source_platform_optimization/feature-oracle.json`.
6. Open `docs/open_source_platform_optimization/progress-log.md`.
7. Open `docs/open_source_platform_optimization/agent-handoff.md`.
8. Open `docs/open_source_platform_optimization/continuity-ledger.md`.
9. Open only the target phase file and its PRIMARY_CONTEXT before planning.

Loop cycle:
- observe
- select
- execute
- verify
- record
- decide

Edit boundaries:
- Prefer no further edits.
- If a validation failure is caused by the new OSP changes, edit only the
  relevant OSP file, CI file, demo-data file, route-smoke file, or report.
- Do not edit unrelated roadmap or production deployment configuration.

Validation:
- `git diff --check`
- `python3 -m json.tool docs/open_source_platform_optimization/feature-oracle.json >/dev/null`
- `python3 -m json.tool docs/open_source_platform_optimization/loop-state.json >/dev/null`
- `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/open_source_platform_optimization --strict --quality-score`
- `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_upgrade --strict --quality-score`
- `docker compose --env-file .env.example config --quiet`
- `bash -n scripts/new/validate-env.sh scripts/new/seed-demo-data.sh`
- `uv run ruff check tests/scripts/test_validate_env_quickstart.py tests/scripts/test_web_runtime_config_entrypoint.py tests/scripts/test_seed_demo_data.py`
- `uv run --extra dev --extra test pytest -q --no-cov tests/scripts/test_validate_env_quickstart.py tests/scripts/test_web_runtime_config_entrypoint.py tests/scripts/test_seed_demo_data.py`
- `pnpm -C web type-check`
- `pnpm -C web lint`
- `pnpm -C web build`
- `pnpm -C web e2e:opensource`
- `scripts/new/seed-demo-data.sh --dry-run`

Evidence:
- Keep `docs/open_source_platform_optimization/reports/osp-04-release-distribution-and-community-readiness-report.md` current.
- Preserve feature-oracle evidence for OSP-F005.
- Record any new terminal verification result in the progress log.

Code summary writeback:
- If final verification changes code or docs, summarize the new code facts in
  `docs/open_source_platform_optimization/source-packet.md` and
  `docs/open_source_platform_optimization/continuity-ledger.md`.

Stop conditions:
- Stop if credentials, registry tokens, external dashboards, production
  deployment, production migrations, destructive commands, or production data
  access are required.
- Do not publish packages, push release tags, deploy environments, rotate
  credentials, run production migrations, or mutate production data without
  explicit owner approval.
- GAA-04 remains the authority for external release gates such as real env
  values, provider/model alignment, registry credentials, and deployment
  topology.
```
