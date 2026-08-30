# Command Catalog

> The canonical way to do every routine thing in this repo. Agents run these commands rather than
> inventing equivalents, so results are comparable across machines and sessions.
> `harness.yml` holds the machine-readable subset; `make harness-check` verifies they still exist.

**Schema:** `harness/commands/v2`

---

## 0. Toolchain

| Tool | Required version / policy | Notes |
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
| Verify the pinned Agent Runtime source release unit | `make agent-runtime-source-contract` |
| Verify the independent local-Runtime CLI | `make independent-cli-gate` |
| Build the independent native CLI inside Docker | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-cli-build-local` |
| Verify the pinned Rust Agent Runtime image | `make agent-runtime-contract` |
| Verify the Agent PostgreSQL ThreadStore | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/path/to/fork make agent-thread-store-contract` |
| Serial single-kernel release contract | `make agent-runtime-release-gate` |
| Digest-pinned current→frozen→current rollback rehearsal | `make agent-runtime-rollback-rehearsal` |
| Build the pinned App Server image inside Docker and probe initialize | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-source-build-local` |
| Build the pinned Rust Agent Runtime image inside Docker | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-build-local` |
| Smoke-test the Rust Runtime in an isolated Docker network | `AI_PLATFORM_AGENT_RUNTIME_IMAGE=... make agent-runtime-smoke` |
| Run real Qwen Responses simple/long/resumed text gate | `ENV_FILE=/path/to/.env AI_PLATFORM_AGENT_RUNTIME_IMAGE=... make agent-runtime-text-gate` |
| Verify CHR-03 tenant/revision-bound read-only capability bridge | `make agent-runtime-readonly-gate` |
| Run Agent tool side-effect and interruption contract gate | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-write-gate` |
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

`*-build-local` means the Docker daemon is local; Cargo runs only in the multi-stage Docker builder.
Do not run host Cargo/Rust commands. `make rust-changed-crate-gate` is a hosted-CI entrypoint on this
machine, not a local acceptance command.

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
| Back up / restore / list backups | `make backup` / `make restore` / `make backup-list` (external `AI_PLATFORM_BACKUP_DIR`, otherwise the XDG/HOME state directory; repository paths are rejected) |

`python -m database.authority migrate` is the only schema writer; `make migrate`,
`scripts/new/migrate.sh`, `database/cli.py`, and
`database/migrate_per_service.py` are compatibility delegates to that command.
The authority recognizes historical filename, numeric, and per-service ledgers
only as adoption input, then freezes them after the baseline marker. Ambiguous
duplicate numeric revisions fail closed with a reconciliation receipt.

The authority acquires one PostgreSQL session advisory lock before bootstrap,
discovery, or DDL. A concurrent runner waits, then reloads the authoritative
ledger and exits as a no-op; connection loss releases the lock.

`make migrate-init` maps to the authority's `init-fresh` command and therefore
requires a truly empty database plus a complete frozen baseline. It never
falls back to directly piping `database/schema.sql`.

Restoring over shared data requires a current backup and explicit approval.

## 4. Development

| Purpose | Command |
| --- | --- |
| One-shot local dev environment | `make dev-setup` |
| Start / stop / reset / inspect dev containers | `make dev-start` / `make dev-stop` / `make dev-reset` / `make dev-status` |
| Run backend services from mounted source with reload | `make dev-compose` |
| Follow mounted-source logs | `make dev-compose-logs` |

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
pnpm -C web exec tsc --noEmit -p tsconfig.app.json --ignoreDeprecations 6.0
pnpm -C web exec tsc --noEmit -p tsconfig.node.json --ignoreDeprecations 6.0
pnpm -C web lint
pnpm -C web build
pnpm -C web i18n:check
```

**Known canonical-script defect (2026-08-29):** `pnpm -C web type-check` currently invokes the solution root
`tsconfig.json` with `files: []` and checks zero files. The two project-specific commands above are
the truthful direct check until ARC-00 repairs the package script.

A green zero-file run is not a pass. The architecture program must fix the canonical script and add
the Web Node unit suite to CI rather than preserving these fallback commands indefinitely.

E2E — **every Playwright spec lives in `web/e2e/`**; see
[`web/e2e/README.md`](../../web/e2e/README.md) for the layout, the five configs, and where run
artifacts go. `make harness-check` fails when a spec, a config, or a screenshot lands elsewhere.

- `pnpm -C web e2e:opensource` — route smoke used in CI, no live provider needed.
- `pnpm -C web e2e` — targets the current singleton local stack via
  `scripts/dev/start_e2e_stack.sh`; the script does not yet prove Compose owner or source SHA, so run
  the integration preflight and do not call it a trusted release gate until ARC-00 fixes that gap.
- `pnpm -C web e2e:headed` — same, with a visible browser.

SDK SSE contract:

- `make sdk-sse-contract` — runs the shared Python and CLI SSE fixture contract; also runs Java
  and Dart native tests when Maven or Dart is installed, otherwise prints an explicit skip.

## 7. Gates

Run the gate that matches what you touched. This table is the intended mapping, but the current
`harness.yml` triggers do not yet select or enforce CI jobs. ARC-00 must make the mapping executable;
until then report which exact checks actually ran rather than claiming “same as CI.”

| You changed | Gate |
| --- | --- |
| Harness files, docs contract | `make harness-check` |
| Agent Runtime source source/schema/SBOM/image lock | `make agent-runtime-source-contract` |
| Agent Rust Runtime image identity | `make agent-runtime-contract` |
| Agent Rust Runtime Docker lifecycle | `AI_PLATFORM_AGENT_RUNTIME_IMAGE=... make agent-runtime-smoke` |
| Agent Runtime real Qwen text path | `ENV_FILE=/path/to/.env AI_PLATFORM_AGENT_RUNTIME_IMAGE=... make agent-runtime-text-gate` |
| Agent Runtime read-only capability bridge | `make agent-runtime-readonly-gate` |
| Agent Runtime tool side-effect safety | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-write-gate` |
| Rust cutover offline/sub-evidence bundle (not a platform release gate) | `make agent-runtime-release-gate` |
| Digest-pinned current→frozen→current rollback | `make agent-runtime-rollback-rehearsal` |
| Agent ThreadStore fresh/idempotent/live contract | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/path/to/fork make agent-thread-store-contract` |
| Agent runtime compatibility surface | `make verify-assistant-runtime-dev` |
| Eval / trace pipeline | `make eval-e1-gate`, then `make verify-eval-dev` for the wider branch gate |
| Agent core / subagents | `make agent-eval-core-gate` |
| Agent Studio | `make verify-agent-studio` |
| Service boundaries, Agent API surface | **KNOWN INSUFFICIENT:** `make test-isolation` lacks static import enforcement; ARC-00 adds `architecture-boundary-gate` |
| Agent compatibility OpenAPI | **KNOWN INSUFFICIENT:** current live test may skip; ARC-00 adds in-process `verify-openapi-contract` |
| RAG retrieval | `make rag-eval-regression-gate` is fixture-only evidence; implementation/live quality require separate ARC-00 gates |
| Knowledge service (real KB suite, imports `knowledge_service`) | `make kb-unit-gate` |
| KB migrations 100–112 + public/main-N-1/split full-chain ledger replay, 097 late-table handoff, 101 dump/restore boundary, and durable embedding-action jobs (needs Postgres with temporary-database privilege plus `pg_dump`/`pg_restore`) | `make kb-migration-gate` |
| KB development fixture structure / manifest hashes / seed drift (`tests/fixtures/eval/rag/golden/**`; not a release claim) | `make kb-golden-gate` |
| KB T0 release evidence (200–400 reviewed cases, source mix, manifest/pointer/reviewed real-corpus baseline binding) | `make kb-release-evidence-gate` |
| Project a golden JSONL into the versioned Postgres store (PRD T0-#2; `--dry-run` first) | `uv run --all-packages python scripts/import_kb_eval_golden.py <jsonl> --dry-run` |
| Qdrant integration smoke (needs `docker-compose.kbms.yml` up; skips when down) | `make kb-integration-smoke` |
| Real-corpus retrieval baseline candidate (live KS; records distribution only, does not approve it) | `make kb-baseline-record KB_BASELINE_DATASET_ID=<id>` |
| RAG eval fixtures / `scripts/eval_rag.py` | `make rag-eval-regression-gate` |
| Deploy scripts, `.env.example`, Compose | `make validate-example-config` |
| Shared/Java/Dart SDK streaming change | `SDK_SSE_CONTRACT_REQUIRE_ALL=1 make sdk-sse-contract` (plain command is optional-tool sub-evidence) |
| Independent CLI config, launcher, provider adapter, package | `make independent-cli-gate` |
| Independent native CLI composed-source artifact | `AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-cli-build-local` |

## 8. What CI runs

`.github/workflows/ci.yml` on push/PR to `main` and `dev` currently runs:

1. **Script Contracts** — shell syntax + focused script tests.
2. **Compose and Harness** — `make validate-example-config`, Compose render, harness contract.
3. **Frontend** — type-check, lint, build, open-source route smoke.
4. **Eval Contract Gates** — `make eval-e1-gate`, `make agent-eval-core-gate`, `make kb-unit-gate`, the KB development-fixture `make kb-golden-gate`, and supporting units. `make kb-release-evidence-gate` stays blocked until reviewed release evidence exists and is an explicit release gate, not a CI structure check.
5. **KB Migration Gate** — `make kb-migration-gate` against a CI Postgres service.
6. **Rust Changed Crate** — locked-toolchain fmt/check/changed-crate tests on the hosted runner.
7. **Release Readiness** — public docs present, demo seed dry run.

Run locally supported gates before saying done. Rust fmt/check/test evidence comes from the hosted CI
job; local acceptance consumes a Docker-built or pulled Runtime/Worker image instead of host Cargo.

This is not yet a complete product release gate: normal Gateway/Knowledge/DB suites and Web Node
units are not comprehensively wired, the RAG gate is fixture replay rather than live execution,
and the current OpenAPI isolation check can skip when Gateway is unavailable. Run the direct gate,
state its evidence tier and skip count, and do not infer unexecuted coverage from CI green.

### Migration 101 is restore-required

Migration 101 replaces the segment conflict identity
`(document_id, position)` with `(document_id, content_type, position)`. An N-1
writer therefore cannot write safely after 101: its old `ON CONFLICT` target no
longer has a matching unique constraint.

Before the serial release window, run `make backup`, verify that the backup
artifact is non-empty/readable, and retain it outside the database being
upgraded. Apply 101 only with the matching application version. Returning to
N-1 requires stopping the new application, restoring the pre-101 dump into a
replacement database, verifying the restored N-1 constraint and retained
rows, and only then starting the old image against that replacement database.
Starting an old image against the post-101 database is not a rollback path.

`make kb-migration-gate` proves both sides with real PostgreSQL: the N-1 upsert
works before 101, fails after 101, and works again only after a real
`pg_dump`/`pg_restore` round trip into a new temporary database. The supported
central-chain entrypoints are `make migrate` and
`python -m database.authority migrate`; `database/run_migration.py` is retired
and always refuses single-file execution before reading credentials. The
historical `database/cli.py` and `database/migrate_per_service.py` names are
thin complete-plan delegates and cannot select a version or service.
