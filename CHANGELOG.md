# Changelog

## Unreleased

### Product convergence (program: deploy/runbooks/product-convergence)

- Removed the Confluence fossil stack: the gateway REST surface (`src/api/v1/confluence.py`,
  `src/api/schemas/confluence.py`) that returned 503 unconditionally since KB was split into a
  microservice, the zero-reference gateway-side sync code (`src/services/knowledge/confluence/`),
  and the console UI (`web/src/pages/confluence/`, the Confluence tab of `/tasks`, and the
  Confluence sync cards in knowledge dataset sources). Confluence connectivity is now exclusively
  the connector stack (`src/api/v1/connectors.py`); `ConnectorsPanel` in the assistant chat was
  re-pointed to the connector catalog/OAuth/MCP APIs.
- Removed the standalone exam management surface (`src/api/v1/exams.py`, `/exams` routes, the
  Services-page exams tab, i18n keys). Exam data tables remain in place; the capability returns
  through the assistant quiz tool and the ai-quiz plugin.
- Trimmed `src/api/v1/quiz.py` to a deprecated shim: deleted `POST /generate/stream`,
  `GET /list`, and `GET /{quiz_id}/attempts/export`; the load-bearing generate/get/submit/
  attempts/delete/share endpoints and the public share routes stay (consumed by the in-chat
  quiz card, QuizShareDialog, and public quiz page).

### Platform architecture

- Added [`docs/harness/platform-architecture.md`](docs/harness/platform-architecture.md): the product
  law behind form and capability diversification — four layers (surfaces / contract / kernel /
  extensions over the gateway), five rules, the `permissions` + `capabilities` coexistence decision,
  and the admission table for what may live in the kernel versus ship as an extension.
- Added the `agent-contract-unification` program under `deploy/runbooks/`: seven phases that make the
  built-in assistant an `AgentSpec` instance, unify subagents into the same type, move builder
  settings out of the chat request, put the console on the public runtime contract, extend the SDKs
  to it, and gate domain logic out of the shared core package.
- `make harness-check` now validates each program against its own `loop-state` schema version rather
  than asserting one contract on all of them, which removed eight permanent warnings.

### Agent harness and documentation

- Added an explicit agent harness: `harness.yml` (machine-readable contract) plus
  `docs/harness/` covering architecture boundaries, the canonical command catalog, the
  working agreement, and the mandatory Docker/secret rules.
- Rewrote `AGENTS.md` as a short tier-1 contract (repo map, canonical commands, gates, safety)
  and added `CLAUDE.md` for Claude Code specifics. Both are line-budgeted.
- Added `make harness-check` (`scripts/harness/check_harness.py`) and wired it into CI: it fails
  when a declared command no longer exists, a required doc is missing, an instruction file exceeds
  its budget, or a harness link breaks.
- Stopped ignoring `docs/` in `.gitignore`. About 100 design, plan, and program files were
  invisible to every other machine and every coding agent; the repository is now the system of
  record. Root-only scratch patterns are anchored so they no longer swallow real documents.
- Reorganized `docs/` into `harness/`, `design/`, `plans/`, `research/`, `architecture/`, and
  dated `archive/`, with `docs/README.md` as the index.
- Consolidated all multi-session programs under `deploy/runbooks/`; `loop-state.json` stays the
  authoritative status. Archived the root `HANDOFF.md`, which depended on a directory outside
  this repository, and `PLATFORM_REVIEW_2026-07-14.md`.

### Workspace hygiene

- `make harness-check` now enforces where files go: Playwright specs only under `web/e2e/`
  (or `web/src/`, `sdk/`), Playwright configs only under `web/`, and no screenshots, archives, or
  stray test files at the repository root. This is what previously let verification screenshots
  accumulate in the root.
- Added [`web/e2e/README.md`](web/e2e/README.md) documenting the E2E layout, the five Playwright
  configs, where run artifacts go, and how to point browser tooling at `tmp/browser/` instead of
  the repository root. Added `web/e2e/fixtures/` for static test data.
- Removed root strays: three verification screenshots, a scratch orchestration script, and the
  `.playwright-mcp/` dump directory.
- Removed `claude-code`, an orphan git submodule reference with no `.gitmodules` entry and an empty
  working directory, and `.kiro/skills/` — 21 tracked symlinks into the gitignored `.agents/`
  directory that were dangling on every checkout, including this one.

### Deployment

- Added `make doctor`: a read-only host preflight covering required tooling, Compose v2, Docker
  memory and disk headroom, host ports, `.env` presence/mode/placeholder keys, and — blocking —
  whether running `ai-gateway-*` containers belong to this checkout.
- Documented the shortest correct startup path as `make doctor && make quickstart && make status`
  in `README.md` and `DEPLOY.md`.
- CI now verifies the harness contract and the syntax of the deploy scripts.

### Runtime

- Bundled document generation as the trusted `ai-docgen` Agent Plugin inside
  the Assistant image and removed the standalone docgen service, image, port,
  signed-download endpoint, and release artifact.
- Isolated Agent Plugin MCP parsing in a focused validation module.

### Open-source standalone release

- Repositioned the project as a standalone AI Gateway platform with a general AI assistant and a knowledge-base service.
- Removed legacy domain-specific application surfaces, mock servers, ingestion scripts, tests, and frontend pages from the default product path.
- Removed private repository memory, stale planning files, generated SDK archives, and outdated product-specific test/refactor artifacts.
- Generalized SDK package names, CLI storage paths, Java namespaces, docgen package naming, sample URLs, Helm defaults, and docgen fixtures.
- Renamed the gateway-authored LangGraph runtime model payload to `gateway_model`.
- Added a root `.env.example` for local Docker deployment without committing real secrets.
- Added open-source governance files for license, contributing, security reporting, support, code of conduct, issue templates, and pull request review expectations.
- Added strict environment validation for required secrets, model keys, embedding keys, provider names, port conflicts, CORS JSON shape, Docker Compose interpolation, and runtime dependency health.
- Updated `README.md` with a downloader-ready quickstart, required env variables, deployment commands, validation commands, microservice communication notes, DB/Qdrant volume caveats, and troubleshooting.
- Added deterministic local demo data with dry-run/apply commands for knowledge-base, public share, quiz, and exam route checks.
- Added a public release checklist that covers local gates, GitHub artifact workflows, post-release smoke checks, and rollback handling.
- Updated CI toward stable open-source contributor checks: script contracts, focused tests, Docker Compose rendering, harness JSON checks, frontend typecheck/lint/build, and release documentation gates.

### Startup and deployment

- Updated `docker-compose.yml` to use the fixed project name `ai-gateway` and project-scoped volumes.
- Switched default storage to local persisted volumes so a first-time open-source deployment does not require S3.
- Changed the default frontend host port to `8081` to avoid privileged/commonly occupied port `80`.
- Added healthy dependency ordering across PostgreSQL, Redis, Qdrant, knowledge service, assistant service, MCP docgen, gateway, and frontend.
- Updated Qdrant health checks to avoid relying on missing container tools.
- Updated gateway health checks to use `/health/ready` instead of shallow process liveness.
- Updated Make targets:
  - `make validate-config`
  - `make validate`
  - `make quickstart`
  - `make stop` now stops containers without deleting volumes.

### Microservice communication

- Standardized Docker-internal service URLs:
  - `ASSISTANT_SERVICE_URL=http://assistant-service:8093`
  - `KB_SERVICE_URL=http://knowledge-service:8092`
  - `MCP_DOCGEN_SERVICE_URL=http://mcp-docgen-server.internal:8765`
- Kept assistant-service private to the Docker network.
- Kept knowledge-service host-bound to `127.0.0.1` for local debugging.
- Added gateway readiness checks for downstream knowledge, assistant, and MCP docgen services.
- Preserved HMAC protection through `GATEWAY_ASSISTANT_SHARED_SECRET` across gateway-to-assistant and gateway-to-knowledge calls.

### Knowledge base

- Simplified the knowledge service toward a provider-neutral KB/RAG surface.
- Removed domain-specific chunking, metadata, authority, synonym, ingestion, and evaluation paths from the default code path.
- Exposed embedding provider configuration through root env variables:
  - `KB_EMBEDDING_PROVIDER`
  - `KB_EMBEDDING_API_KEY`
  - `KB_EMBEDDING_MODEL`
  - `KB_EMBEDDING_DIMENSION`
- Added compose mappings for Gemini, DashScope, and SiliconFlow embedding keys.

### Assistant

- Kept the general assistant service as an extracted microservice behind the gateway.
- Continued to pass common provider keys into gateway, assistant, and docgen containers where relevant.
- Retained frontend and SDK paths around the general assistant and knowledge-base flows.

### Safety

- Ensured root `.env` remains ignored.
- Ensured `.env.example` is explicitly committable.
- Avoided printing secret values from the validator.
- Added runtime checks for authenticated PostgreSQL and Redis access rather than only checking container liveness.
