# Command Catalog

> The canonical way to do every routine thing in this repo. Agents run these commands rather than
> inventing equivalents, so results are comparable across machines and sessions.
> `harness.yml` holds the machine-readable subset; `make harness-check` verifies they still exist.

**Schema:** `harness/commands/v1`

---

## 0. Toolchain

| Tool | Pinned version | Notes |
| --- | --- | --- |
| Python | ≥ 3.10 (CI uses 3.12) | Managed by `uv`; the workspace is defined in `pyproject.toml` |
| uv | latest | `uv run --all-packages` resolves the whole workspace |
| Node | 22 | Frontend only |
| pnpm | 10.33.0 via corepack | `corepack prepare pnpm@10.33.0 --activate` |
| Docker | Compose v2 | `docker compose`, not `docker-compose`, wherever possible |

Never mix package managers: **pnpm for `web/`, uv for Python.** No `npm install`, no bare `pip install`.

## 1. Machine and stack

| Purpose | Command |
| --- | --- |
| Preflight the machine before anything else | `make doctor` |
| Verify the pinned Codex Harness release unit | `make codex-harness-contract` |
| Verify the pinned Rust Agent Runtime image | `make codex-runtime-contract` |
| Verify the Codex PostgreSQL ThreadStore | `CODEX_HARNESS_FORK=/path/to/fork make codex-thread-store-contract` |
| Build the pinned local Codex image and probe initialize | `CODEX_HARNESS_FORK=/absolute/fork make codex-harness-build-local` |
| Build the pinned Rust Agent Runtime image | `CODEX_HARNESS_FORK=/absolute/fork make codex-runtime-build-local` |
| Smoke-test the Rust Runtime in an isolated Docker network | `CODEX_RUNTIME_IMAGE=... make codex-runtime-smoke` |
| First run: generate `.env`, pull images, start, migrate, validate | `make quickstart` |
| Maintainer first run from current source | `make quickstart-build` |
| Validate `.env` + Compose without starting | `make validate-config` |
| Validate config **and** running runtime | `make validate` |
| Validate the committed public example only | `make validate-example-config` |
| Health table (nonzero exit on failure — usable as a release gate) | `make status` |
| Live logs | `make logs` |
| Stop / restart without deleting data | `make stop` / `make restart` |
| Apply changed Python source without rebuilding | `make hot-update` |
| Preview / load local demo data | `make seed-demo` / `make seed-demo-apply` |

Every target above accepts `ENV_FILE=/path/to/.env` when the real env file lives outside the repo.

## 2. Deployment

| Purpose | Command |
| --- | --- |
| Deploy everything (no forced rebuild) | `make deploy` |
| Deploy and rebuild images | `make deploy-build` |
| Deploy using China mirrors | `make deploy-cn` |
| Infrastructure only (postgres/redis/qdrant) | `make deploy-infra` |
| Application services only | `make deploy-app` |
| Application services, skip migrations | `make deploy-app ARGS="--no-migrate"` |

`deploy-infra` and `deploy-app` are mutually exclusive; run them as separate commands.

## 3. Database

| Purpose | Command |
| --- | --- |
| Run pending migrations | `make migrate` |
| Initialize schema on a fresh database | `make migrate-init` |
| Show applied vs pending | `make migrate-status` |
| Back up / restore / list backups | `make backup` / `make restore` / `make backup-list` |

Restoring over shared data requires a current backup and explicit approval.

## 4. Development

| Purpose | Command |
| --- | --- |
| One-shot local dev environment | `make dev-setup` |
| Start / stop / reset / inspect dev containers | `make dev-start` / `make dev-stop` / `make dev-reset` / `make dev-status` |
| Run backend services from mounted source with reload | `make dev-compose` |
| Follow mounted-source logs | `make dev-compose-logs` |
| Enable / test / disable the trusted local code sandbox | `make code-executor-enable` / `-test` / `-disable` |

## 5. Python tests and lint

Always scope to the paths you touched — the full suite is long.

```bash
uv run --all-packages --extra test pytest -q --no-cov tests/<area>/test_<file>.py
```

```bash
uv run --all-packages --extra dev ruff check <paths>
```

Notes:

- `--no-cov` is required for scoped runs; the project sets `--cov-fail-under=25` globally.
- `asyncio_mode = auto` — do not add `@pytest.mark.asyncio`.
- Tests marked `integration` need a live stack; state clearly when you skip them.

## 6. Frontend

```bash
pnpm -C web type-check
pnpm -C web lint
pnpm -C web build
pnpm -C web i18n:check
```

E2E — **every Playwright spec lives in `web/e2e/`**; see
[`web/e2e/README.md`](../../web/e2e/README.md) for the layout, the five configs, and where run
artifacts go. `make harness-check` fails when a spec, a config, or a screenshot lands elsewhere.

- `pnpm -C web e2e:opensource` — route smoke used in CI, no live provider needed.
- `pnpm -C web e2e` — full stack E2E via `scripts/dev/start_e2e_stack.sh`.
- `pnpm -C web e2e:headed` — same, with a visible browser.

SDK SSE contract:

- `make sdk-sse-contract` — runs the shared Python and CLI SSE fixture contract; also runs Java
  and Dart native tests when Maven or Dart is installed, otherwise prints an explicit skip.

## 7. Gates

Run the gate that matches what you touched. These are the same commands CI runs.

| You changed | Gate |
| --- | --- |
| Harness files, docs contract | `make harness-check` |
| Codex Harness source/schema/SBOM/image lock | `make codex-harness-contract` |
| Codex Rust Runtime image identity | `make codex-runtime-contract` |
| Codex Rust Runtime Docker lifecycle | `CODEX_RUNTIME_IMAGE=... make codex-runtime-smoke` |
| Codex ThreadStore fresh/idempotent/live contract | `CODEX_HARNESS_FORK=/path/to/fork make codex-thread-store-contract` |
| Assistant runtime | `make verify-assistant-runtime-dev` |
| Eval / trace pipeline | `make eval-e1-gate`, then `make verify-eval-dev` for the wider branch gate |
| Agent core / subagents | `make agent-eval-core-gate` |
| Agent Studio | `make verify-agent-studio` |
| Service boundaries, assistant API surface | `make test-isolation` |
| Assistant OpenAPI | `make snapshot-assistant-openapi` then `make test-isolation` |
| RAG retrieval | `make rag-eval-regression-gate` |
| Deploy scripts, `.env.example`, Compose | `make validate-example-config` |
| Python / CLI / Java / Dart SDK streaming | `make sdk-sse-contract` |

## 8. What CI runs

`.github/workflows/ci.yml` on push/PR to `main` and `dev`:

1. **Script Contracts** — shell syntax + focused script tests.
2. **Compose and Harness** — `make validate-example-config`, Compose render, harness contract.
3. **Frontend** — type-check, lint, build, open-source route smoke.
4. **Eval Contract Gates** — `make eval-e1-gate`, `make agent-eval-core-gate`, supporting units.
5. **Release Readiness** — public docs present, demo seed dry run.

A change that cannot pass these locally will not pass in CI. Run the matching gate before saying done.
