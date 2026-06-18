# Changelog

## Unreleased

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
