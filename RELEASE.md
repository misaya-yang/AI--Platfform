# Release Checklist

This repository publishes an open-source AI Gateway platform with Docker images
and SDK packages. A release is ready only when the checks below pass on the
exact commit being tagged.

## Pre-Release Gates

Run these checks locally before creating a tag:

```bash
scripts/new/validate-env.sh --env .env.example --config-only
docker compose --env-file .env.example config --quiet
pnpm -C web type-check
pnpm -C web lint
pnpm -C web build
pnpm -C web e2e:opensource
uv run --extra dev --extra test pytest -q --no-cov \
  tests/scripts/test_validate_env_quickstart.py \
  tests/scripts/test_web_runtime_config_entrypoint.py \
  tests/scripts/test_seed_demo_data.py
scripts/new/seed-demo-data.sh --dry-run
```

For Codex-assisted release work, also validate the phase harnesses locally:

```bash
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_upgrade --strict --quality-score
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/open_source_platform_optimization --strict --quality-score
```

The absolute Codex path above is a maintainer-local gate and is not required for
external contributors.

## Versioning

- Application releases use tags like `v2.0.0`.
- SDK package releases use tags like `sdk-v1.0.2`.
- Update `CHANGELOG.md` before tagging.
- Keep `pyproject.toml`, `web/package.json`, SDK package metadata, and image
  tags aligned when the public API or package surface changes.

## Artifact Workflows

- `.github/workflows/docker-publish.yml` runs on `v*` tags and publishes:
  - `ghcr.io/<owner>/ai-gateway`
  - `ghcr.io/<owner>/ai-gateway-web`
  - `ghcr.io/<owner>/islamic-content-service`
- `.github/workflows/publish-sdk.yml` runs on `sdk-v*` tags and publishes:
  - `sdk/python` to PyPI
  - `sdk/cli` to npm

Before relying on these workflows, confirm the target repository has the
required package permissions and registry tokens configured in GitHub Actions.
Do not commit tokens or registry credentials.

## Tagging

```bash
git status --short
git log --oneline -5
git tag -a v2.0.0 -m "Release v2.0.0"
git push origin v2.0.0
```

Use an SDK tag only when the SDK package metadata has changed:

```bash
git tag -a sdk-v1.0.2 -m "Release SDK v1.0.2"
git push origin sdk-v1.0.2
```

## Post-Release Smoke

After images or packages publish, verify:

```bash
docker pull ghcr.io/<owner>/ai-gateway:<version>
docker pull ghcr.io/<owner>/ai-gateway-web:<version>
docker pull ghcr.io/<owner>/islamic-content-service:<version>
```

Then run the local quickstart against the release image set or a clean checkout:

```bash
make validate-config
make quickstart
make seed-demo
make status
```

## Rollback

If release validation fails before tagging, do not tag. Fix forward on the
branch or revert the release-preparation commit.

If a tag has been pushed but artifacts fail:

1. Leave the failed tag in place unless the repository owner explicitly approves
   deleting it.
2. Document the failed workflow URL in `CHANGELOG.md` or a GitHub release note.
3. Commit the fix.
4. Publish a new patch tag such as `v2.0.1`.

If a deployed environment is already using a bad image:

1. Stop rollout automation.
2. Restore the previous known-good image tag.
3. Run `make validate` and `make status` against the environment.
4. Keep traffic closed until gateway readiness, frontend health, and dependency
   checks are all passing.

## Release Blockers

- Missing or placeholder secrets in a non-local env file.
- CI failing on the release commit.
- Docker Compose config failing to render from `.env.example`.
- Frontend build/typecheck/lint failure.
- Demo seed dry-run failure.
- Undocumented production migration or destructive data operation.
- Unresolved security report affecting the release surface.
