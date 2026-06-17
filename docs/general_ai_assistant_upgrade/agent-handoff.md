# General AI Assistant Upgrade Harness Agent Handoff

**Created:** 2026-06-16

**Harness Folder:** `docs/general_ai_assistant_upgrade`

## Planner Notes

- Baseline phase `GAA-00` is complete.
- Assistant core phase `GAA-01` is complete for the artifact schema-missing read contract.
- Assistant user experience phase `GAA-02` is complete with route E2E and full site walkthrough evidence.
- Assistant eval and safety phase `GAA-03` is complete with deterministic local evidence and no live provider calls.
- Current phase: `GAA-04` is blocked after partial release-readiness validation.
- Next phase file: `docs/general_ai_assistant_upgrade/phase-04-deployment-readiness.md`.
- Next feature-oracle item: `GAA-F005`.
- Start by reading the GAA-04 report and phase contract, then provide `.env`/provider credentials before re-running formal release gates.

## Generator Notes

- Route-access slice is implemented and passing in `web/e2e/support/helpers.ts` and `web/e2e/chat-experience.spec.ts`.
- GAA-03 eval/safety contracts are implemented in `tests/services/assistant/test_eval_safety_contracts.py`.
- GAA-04 static compose, local container health, gateway/frontend health, and expanded browser smoke have evidence.
- Production compose private-port exposure now has static regression coverage: PostgreSQL, Redis, Qdrant, Tempo, knowledge-service, and MCP docgen host ports stay bound to `127.0.0.1`; assistant-service has no host `ports` mapping and remains Docker-network-only through `expose: 8093`.
- `web/e2e/site-walkthrough.spec.ts` now covers visible sidebar routes plus `/knowledge/create`, `/exams`, and the current user's `/users/:userId/edit`; it also auto-adds `/knowledge/:datasetId`, `/exams/:examId`, `/share/:shareId`, and `/quiz/:shareCode` when backing records exist. The latest run passed 1/1 and produced screenshots for 11 pages under `web/test-results/site-walkthrough-walk-main-5358e--detect-obvious-UI-breakage-chromium/` because read-only seed probes found zero datasets, exams, quizzes, and conversation shares.
- `web/e2e/dynamic-route-render.spec.ts` now provides non-mutating mocked render coverage for `/knowledge/:datasetId`, `/exams/:examId`, `/share/:shareId`, and `/quiz/:shareCode`; it passes 2/2 and should be treated as page render confidence, not as a replacement for live seeded-record release coverage.
- Dynamic data probing exposed `/api/v1/exams` 500s and `/api/v1/assistant/quiz/list` 503/500s when quiz/exam migrations were absent. `src/api/v1/exams.py` and `src/api/v1/quiz.py` now return empty read-only lists for missing `exams`/`quizzes` tables, while quiz generation still requires model registry; focused tests are in `tests/api/test_quiz_exam_readiness.py`.
- GAA-04 status observability was improved in `scripts/new/status.sh`; `make status` now reports all local service health without root `.env` and exits nonzero if any health check is unavailable.
- Gateway `/metrics` now has a release smoke helper in `scripts/new/common.sh`; it requires Prometheus markers plus `gateway_up`, rejects empty or generic metrics output, `validate-env.sh --runtime` waits for it after gateway readiness, and `status.sh` reports `Gateway metrics`.
- Source-level Prometheus export is no longer empty on fresh startup: `src/core/observability/metrics.py` registers request/error/latency/billing/connection/circuit-breaker metrics plus `gateway_up`, and preserves histogram labels. Rebuild the gateway before expecting the running Docker service to show this new baseline output.
- Direct status/runtime helpers now probe knowledge-service and assistant-service `/health/ready`; MCP docgen remains on `/health` because it does not expose `/health/ready`.
- GAA-04 microservice failure-isolation baseline improved: non-destructive live-stack mode now passes the per-route circuit-breaker isolation check against public `/api/v1/health`; compose-backed stop/start isolation now passes for assistant-service outage, knowledge-service outage, gateway restart drain, and breaker isolation, with only `langgraph-agent` skipped because it is absent from the local compose stack.
- Assistant OpenAPI contract now passes through in-process dev-only OpenAPI generation when no gateway secret is configured.
- Assistant black-box isolation tests now default to private topology and only require direct assistant-service host reachability when `ASSISTANT_REQUIRE_DIRECT=true`.
- Assistant config no longer advertises a hardcoded unavailable default model; it returns a default only when `/assistant/models` has a configured-provider model visible to the user.
- Current runtime has only OpenAI configured but enabled assistant models are DashScope/Google, so `/assistant/models` is empty and credential-injected black-box chat fails before model calls.
- `scripts/new/validate-env.sh` now has a runtime model/provider alignment gate; once `.env` exists, `make validate` should catch the current OpenAI-vs-DashScope/Google mismatch.
- The validate-env gate is covered by `tests/scripts/test_validate_env_quickstart.py` for config-only, mismatch, matching-provider runtime, Vertex chat runtime, and docs/compose/env injection paths.
- `tests/scripts/test_validate_env_quickstart.py` now passes 58/58 and locks the production compose private-port boundary, HTTPS CORS release-origin boundary, HTTPS non-local docgen public URL boundary, non-local frontend browser runtime URL boundary, frontend auth-domain alignment boundary, support-email release-readiness boundary, strict gateway metrics runtime/status boundary requiring `gateway_up`, direct knowledge/assistant readiness helper boundary, quickstart migration sequencing, base schema initialization before numbered migrations, migration init long-output safety, and status nonzero failure boundary.
- `tests/scripts/test_web_runtime_config_entrypoint.py` covers frontend container `runtime-config.js` generation with safe defaults, support email defaulting from auth domain, JS escaping for quotes/backslashes/line breaks, and nginx no-store delivery for `/runtime-config.js`; combined script/runtime-config tests pass 62/62 with the strict gateway metrics runtime/status check, direct knowledge/assistant readiness helper coverage, quickstart migration sequencing, base schema initialization before numbered migrations, migration init long-output safety, and status nonzero failure coverage.
- `GOOGLE_CHAT_BACKEND` and `VERTEX_CHAT_API_KEY` are now present in `.env.example`, README, and gateway/assistant-service compose env injection, matching the shared Google resolver and validator.
- `validate-env.sh` now mirrors `resolve_google("chat")` for Vertex provider inference in both config-only and runtime gates: `GOOGLE_CHAT_BACKEND=vertex` with `VERTEX_CHAT_API_KEY` makes `google` and `google-vertex` valid; `VERTEX_CHAT_API_KEY` without a Vertex backend is not treated as configured.
- `DOCGEN_ARTIFACT_SIGN_KEY` is now a required config secret. Without it, docgen falls back to a per-process random key and signed artifact URLs break across restarts, so release validation must fail early.
- `DOCGEN_PUBLIC_URL` now fails full app validation when it remains HTTP, localhost, or loopback under a non-local auth domain, so signed artifact links must be HTTPS and browser-reachable in shared deployments.
- `VITE_API_URL`, `VITE_API_BASE_URL`, and `VITE_TELEMETRY_ENDPOINT` now fail full app validation when they are explicit HTTP, localhost, or loopback absolute URLs under a non-local auth domain; blank values and same-origin paths such as `/api` and `/telemetry` remain valid for nginx proxy deployments.
- `VITE_AUTH_EMAIL_DOMAIN` now fails full app validation when it is explicitly set to a different value than `AUTH_ALLOWED_EMAIL_DOMAIN` under a non-local auth domain; omit it to rely on the Compose fallback, or set it to the same backend-accepted domain so username-only login remains usable.
- `VITE_SUPPORT_EMAIL` now defaults to `admin@AUTH_ALLOWED_EMAIL_DOMAIN` through Compose and `admin@VITE_AUTH_EMAIL_DOMAIN` in the runtime-config entrypoint when blank; explicit `example.com` support mailboxes fail full app validation under non-local auth domains.
- `validate-env.sh --infra-only --config-only` is scoped to PostgreSQL, Redis, and Qdrant secrets/ports plus service-filtered compose config; it no longer requires app-only secrets before infrastructure-only deployment prep.
- `validate-env.sh --infra-only --config-only` now uses no-interpolate compose structure validation for the infrastructure subset, so missing app-only compose vars do not block infra-only checks.
- `scripts/new/deploy.sh --app` now deploys gateway, frontend, knowledge-service, assistant-service, and mcp-docgen-server. This prevents app-only updates from leaving changed application microservices stale.
- `scripts/new/deploy.sh --pull` now respects the selected service scope after `--infra` or `--app`, preventing unrelated image pulls during partial deployments.
- `scripts/new/deploy.sh --infra --app` now exits before pre-flight checks because infra-only and app-only are mutually exclusive partial-deploy modes.
- `scripts/new/deploy.sh --app` runs migrations by default and now waits for PostgreSQL before doing so. Use `make deploy-app ARGS="--no-migrate"` when the operator wants restart-only app deployment.
- `scripts/new/deploy.sh --env` now rejects missing file paths before Docker pre-flight, and `scripts/new/deploy.sh --env FILE` rejects missing selected env files before pre-flight.
- `scripts/new/deploy.sh` now uses `--env-file "$ENV_FILE"` for the final Compose `ps` summary, so external-env deployments do not fall back to root `.env` at the last reporting step.
- `scripts/new/migrate.sh --auto` now checks core base schema tables and applies `database/schema.sql` before numbered migrations when the base schema is missing.
- `scripts/new/migrate.sh --init` now captures schema output before previewing the first 30 SQL status lines, avoiding pipefail false failures when schema output is long.
- `scripts/new/migrate.sh --auto` now fails deployment on migration SQL errors instead of continuing after an unrecorded failed migration.
- `scripts/new/migrate.sh` now handles nonzero `psql`/`run_sql_file` exits explicitly before checking `ERROR` lines, so real SQL failures still print the script's migration failure message.
- `scripts/new/migrate.sh` now rejects unknown options, missing `--env` values, and missing explicit env files before migration status/init/auto logic. The reviewed failure case was `migrate.sh --env /tmp/missing --status`, which previously reached default database status output.
- `scripts/new/common.sh` now runs migration SQL files through `psql -v ON_ERROR_STOP=1` for Docker and local psql paths, so SQL file execution stops on the first error.
- `scripts/new/backup.sh` now accepts `--env FILE`, rejects unknown options and missing `--env` values before action, requires the selected env file before creating `backups/` or running backup/restore database operations, and restores with `psql -v ON_ERROR_STOP=1` so rollback SQL errors do not report success.
- Makefile targets now support `ENV_FILE=/path/to/.env` for validation, deployment, status, migration, quickstart, and dev-compose. `scripts/new/deploy.sh` and `scripts/new/migrate.sh` also accept `--env FILE`.
- `make quickstart` now waits for PostgreSQL and runs `migrate.sh --env "$(ENV_FILE)" --auto` before runtime validation, so first local startup does not skip pending migrations.
- `scripts/new/status.sh` now calls Compose service listing with `--env-file "$ENV_FILE"` before fallback, so status uses the same external env path selected by Makefile; it exits nonzero if any health check is unavailable. The deploy summary has the same env-file boundary for Compose `ps`.
- Makefile stop/restart/logs, backup/restore/list, and dev helper targets also forward `ENV_FILE`.
- `scripts/new/setup-dev.sh` now accepts direct `--env FILE`, parses it before loading env, rejects missing explicit env files before status/action, lets help/status/stop work without dev `POSTGRES_PASSWORD`/`REDIS_PASSWORD`, still requires those secrets before start/full/reset, honors `QDRANT_HTTP_PORT`, and forwards `ENV_FILE` to child migrations.
- `bash scripts/new/setup-dev.sh --env /Users/.../hejaz_projects/ai_gateway/ai-gateway/.env --status` passed as a non-destructive external-env status check without printing secret values.
- `bash scripts/new/setup-dev.sh --env /tmp/ai-platform-missing-env-for-setup-dev --status` passed as a negative safety check: it exited 1 before status/action because the selected env file did not exist.
- All phase contracts now include strict-validator review evidence and minimal-change scope gates; terminal GAA-04 also includes whole-demand regression. Harness validation passes with quality score 100.
- `validate-env.sh` skips Compose interpolation when required config checks already failed, so missing/placeholder release variables are the first actionable output.
- `validate-env.sh --env` now rejects missing file paths before config/runtime validation.
- Full app `validate-env.sh` now requires explicit HTTPS CORS origin arrays for non-local auth domains, reports wildcard origin failures explicitly, and rejects non-http, HTTP, and localhost origins when `AUTH_ALLOWED_EMAIL_DOMAIN` is non-local.
- `web/docker-entrypoint.d/40-runtime-config.sh` keeps the production `/usr/share/nginx/html/runtime-config.js` output path by default and accepts `RUNTIME_CONFIG_OUTPUT_PATH` only for controlled script tests.
- `web/nginx.conf` serves `/runtime-config.js` from an exact no-store location so deploy-time env changes are not cached like immutable frontend assets.
- Makefile deployment targets forward `ARGS` to `scripts/new/deploy.sh`; `make deploy ARGS="--build --no-migrate"` and `make deploy-app ARGS="--no-migrate"` dry runs show the options are passed.
- Assistant frontend now disables the composer/send path and has a send-handler guard when no model is available.
- `web/e2e/chat-experience.spec.ts` now has a no-model test that must be run against current source, not the stale 8081 frontend container, unless that container is rebuilt.
- Formal `make validate-config` and `make validate` are blocked by missing root `.env`.
- The supplied external env at `/Users/.../hejaz_projects/ai_gateway/ai-gateway/.env` is readable via formal Makefile `ENV_FILE=...` targets, but full release validation is still blocked by missing, placeholder, or invalid `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`, `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`, `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.
- GAA-04 should be re-run after `.env` and provider credentials are supplied, without deploying unless explicitly approved.
- Preserve protected route behavior, runtime config precedence, SSE parser behavior, telemetry guards, assistant route state, disabled optional Confluence empty-state behavior, API key legacy schema compatibility, and GAA-03 safety contracts.

## Evaluator Notes

- Review GAA-04 follow-up for secret exposure, destructive Docker/data operations, migration risk, public port exposure, health false positives, rollback gaps, and browser/runtime evidence gaps. The walkthrough can now cover knowledge/exam detail, public share, and quiz-taking route families when records exist, but current seed probes found none, so those route families still need seeded-record smoke coverage or an explicit waiver before launch.
- For microservice isolation, distinguish the passing non-destructive live-stack breaker check from the compose-backed stop/start suite. The local stop/start suite now passes except for the skipped `langgraph-agent` case, which needs a stack containing that service or a launch waiver if LangGraph is in scope.
- Re-run GAA-03 eval tests if GAA-04 changes assistant runtime, middleware, guardrails, tool formatting, memory persistence, or release scripts that exercise those paths.
- Do not accept GAA-F005 as passing without config/runtime/browser/rollback evidence or explicit user waiver for blocked gates.

## Next Handoff

- Active role: generator/evaluator.
- Active phase: GAA-04.
- Current blocker: root `.env` is still absent, and the supplied external env is incomplete for full validation, including explicit HTTPS CORS origins, HTTPS browser-facing `DOCGEN_PUBLIC_URL`, frontend auth-domain alignment if `VITE_AUTH_EMAIL_DOMAIN` is explicitly set, non-example support email if `VITE_SUPPORT_EMAIL` is explicitly set, and safe frontend browser runtime URL values if `VITE_API_URL`, `VITE_API_BASE_URL`, or `VITE_TELEMETRY_ENDPOINT` are explicitly set; aligned provider credentials/model rows, assistant black-box credentials, live seeded knowledge/exam detail plus public share/quiz-taking records or waiver, optional `langgraph-agent` stop/start evidence or waiver if that service is launch-scoped, and deployment approval are still missing or unproven. Gateway metrics smoke and direct knowledge/assistant readiness smoke are wired, but full `make validate` still cannot reach runtime checks until config validation passes. Current read-only probes returned zero datasets, exams, quizzes, and conversation shares; mocked dynamic-route render smoke passes but does not prove live seeded-record journeys.
- Required evidence before completion: passing `make validate-config`, passing `make validate`, acceptable service-isolation result or waiver, browser screenshot/Playwright evidence including seeded dynamic routes or waiver, rollback command list tied to a release artifact/tag, launch/block decision, source-packet update, continuity-ledger update, and oracle evidence.
