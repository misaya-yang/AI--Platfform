# AI Gateway

AI Gateway is an open-source AI application platform for gateway routing, a general AI assistant, and a knowledge-base service backed by PostgreSQL, Redis, and Qdrant.

The default Docker setup is intended for a local first run: after you fill your own model keys and embedding key, `make quickstart` builds and starts the frontend, gateway, assistant service, knowledge service, document-generation MCP service, PostgreSQL, Redis, and Qdrant.

## Services

| Service | Role | Default URL |
| --- | --- | --- |
| Frontend | Web console | `http://localhost:8081` |
| Gateway | Public API, auth, proxy, sessions | `http://localhost:8080` |
| Assistant service | General AI assistant runtime | Internal: `http://assistant-service:8093` |
| Knowledge service | KB CRUD, document ingestion, retrieval | `http://localhost:8092` |
| MCP docgen server | Document generation tool server | `http://localhost:8765` |
| PostgreSQL | Primary relational database | `127.0.0.1:5432` |
| Redis | Cache, sessions, queues | `127.0.0.1:6379` |
| Qdrant | Vector database | `127.0.0.1:6333` |

## Quick Start

Prerequisites:

- Docker and Docker Compose
- `make`
- At least one chat model API key
- One embedding model API key for the knowledge base

1. Copy the example environment:

```bash
cp .env.example .env
```

2. Generate local secrets and replace every `change_me_*` value:

```bash
openssl rand -hex 32
```

Never commit your `.env` file. Only `.env.example` is intended to be committed.

Required values:

- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `JWT_SECRET`
- `GATEWAY_ASSISTANT_SHARED_SECRET`
- `AUTH_ALLOWED_EMAIL_DOMAIN`
- `DEFAULT_USER_PASSWORD` (the example value matches the local bootstrap admin; rotate it before shared or non-local deployments)
- At least one chat key: `DASHSCOPE_CHAT_API_KEY`, `DASHSCOPE_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY`
- `KB_EMBEDDING_PROVIDER`: `gemini`, `dashscope`, or `siliconflow`
- `KB_EMBEDDING_API_KEY`
- `KB_EMBEDDING_MODEL`
- `KB_EMBEDDING_DIMENSION`

3. Validate configuration before starting:

```bash
make validate-config
```

4. Build, start, and run runtime validation:

```bash
make quickstart
```

5. Open the app:

- Frontend: `http://localhost:8081`
- API docs: `http://localhost:8080/docs`
- Gateway readiness: `http://localhost:8080/health/ready`

The local bootstrap admin is `admin@example.com` with password `ChangeMe-Admin-2026!`.
The validator allows that documented local bootstrap password but warns about it.
For non-local deployments, change `AUTH_ALLOWED_EMAIL_DOMAIN`, create your own
administrator, and rotate the bootstrap password immediately.

The example env also sets generous localhost rate limits such as
`RATE_LIMIT_IP_LIMIT`, `RATE_LIMIT_NORMAL_LIMIT`, and
`RATE_LIMIT_ASSISTANT_CHAT_LIMIT` so first-run page loads and assistant testing
do not trip 429s. Tighten them for shared or production deployments.

## Validation Commands

Use these commands when debugging startup or before publishing a deployment:

```bash
make validate-config   # checks .env and docker compose config only
make validate          # checks .env plus running Postgres, Redis, Qdrant, and services
make status            # prints compose state and service health
docker compose ps
```

The validator intentionally does not print secret values. It fails fast on missing keys, placeholder values, weak secrets, duplicate ports, invalid embedding provider names, failed compose interpolation, failed authenticated PostgreSQL/Redis checks, and failed Qdrant/service health checks. The documented local bootstrap admin password is accepted for quickstart and reported as a warning.

## Development Without Rebuilding

`make quickstart` builds the Docker images for the first local run. After that,
backend code changes do not need a full image rebuild during development.

Use source-mounted app services instead:

```bash
make dev-compose
make dev-compose-logs
```

This uses `docker-compose.dev.yml` to bind-mount the gateway, assistant-service,
knowledge-service, and shared core package source into the existing containers
and runs the Python services with `uvicorn --reload`. Rebuild only when
dependencies, Dockerfiles, or image-level system packages change.

## Release Notes

See `CHANGELOG.md` for the module-level summary of the current open-source standalone release cleanup.

## Deployment Commands

```bash
make deploy        # build/start/migrate/health-check all services
make deploy-build  # force rebuild
make deploy-infra  # start only postgres, redis, qdrant
make deploy-app    # start app services and their compose dependencies
make stop          # stop containers without deleting volumes
```

For a destructive local reset, use Docker Compose explicitly and understand that this deletes local database/vector/cache data:

```bash
docker compose down -v --remove-orphans
```

## Microservice Communication

The compose file wires services through Docker DNS names:

- Gateway to assistant: `ASSISTANT_SERVICE_URL=http://assistant-service:8093`
- Gateway/assistant to knowledge service: `KB_SERVICE_URL=http://knowledge-service:8092`
- Knowledge service to Qdrant: `http://qdrant:6333`
- Gateway/assistant/knowledge to PostgreSQL and Redis through internal service names
- MCP docgen server uses the internal alias `mcp-docgen-server.internal`

Gateway-to-assistant and gateway-to-knowledge requests are protected by `GATEWAY_ASSISTANT_SHARED_SECRET` HMAC verification. Use the same value across gateway, assistant service, and knowledge service.

The CORS env values used by compose are JSON array strings:

```env
KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON=["http://localhost:8081","http://localhost:3000"]
ASSISTANT_CORS_ALLOW_ORIGINS_JSON=["http://localhost:8081","http://localhost:3000"]
```

Compose startup waits for infrastructure and microservices to become healthy before starting dependent services. Gateway health uses `/health/ready`, not only a shallow process liveness endpoint.

## Knowledge Base

The knowledge service supports document ingestion, chunking, vector indexing, and retrieval modes including keyword, vector, hybrid RRF, optional reranking, and MMR.

Embedding configuration is controlled by:

```env
KB_EMBEDDING_PROVIDER=gemini
KB_EMBEDDING_API_KEY=...
KB_EMBEDDING_MODEL=gemini-embedding-2-preview
KB_EMBEDDING_DIMENSION=1024
```

Provider notes:

- `gemini`: set `KB_EMBEDDING_API_KEY` to your Google/Gemini embedding key.
- `dashscope`: set `KB_EMBEDDING_PROVIDER=dashscope`, use a DashScope embedding model, and set `KB_EMBEDDING_API_KEY`.
- `siliconflow`: set `KB_EMBEDDING_PROVIDER=siliconflow`, use a SiliconFlow embedding model, and set `KB_EMBEDDING_API_KEY`.

## General AI Assistant

The assistant service is private to the Docker network. Public assistant traffic should go through the gateway/frontend path.

Set at least one chat provider key in `.env`. The runtime currently passes these provider variables through to gateway, assistant service, and docgen where relevant:

- `DASHSCOPE_API_KEY`
- `DASHSCOPE_CHAT_API_KEY`
- `GOOGLE_API_KEY`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `DEEPSEEK_API_KEY`

## Database and Qdrant Notes

The compose project name is fixed to `ai-gateway`, and volumes are project-scoped:

- `ai-gateway_pg-data`
- `ai-gateway_redis-data`
- `ai-gateway_qdrant-data`
- `ai-gateway_kb-data`

PostgreSQL initializes its password only when the volume is first created. If you change `POSTGRES_PASSWORD` after the first run while keeping the old volume, authentication can fail even though `.env` looks correct. For local development, stop the stack and intentionally reset volumes:

```bash
docker compose down -v --remove-orphans
```

Qdrant data is also volume-backed. If you change embedding dimensions or providers for existing collections, create a new collection/dataset or reset the Qdrant volume in local development.

## Troubleshooting

Configuration fails:

```bash
make validate-config
```

Runtime fails:

```bash
make status
docker compose logs --tail=200 gateway
docker compose logs --tail=200 knowledge-service
docker compose logs --tail=200 assistant-service
docker compose logs --tail=200 qdrant
```

Common causes:

- `.env` still contains `change_me_*` placeholders.
- Only a chat key is configured; the knowledge base also needs `KB_EMBEDDING_API_KEY`.
- PostgreSQL password was changed after the first volume initialization.
- Qdrant is running with old vectors from a different embedding dimension.
- `FRONTEND_PORT`, `GATEWAY_PORT`, `POSTGRES_PORT`, `REDIS_PORT`, or `QDRANT_HTTP_PORT` conflicts with another local process.

## Identity and Sessions

The gateway resolves user identity server-side and enforces per-user session isolation.

- Anonymous browser users receive an `HttpOnly` cookie, default `ag_anon_id`.
- Non-browser clients can use the anonymous ID header, default `X-AG-Anonymous-Id`.
- Authenticated users are resolved from JWT or API key at the gateway layer.
- `user_id` and `tenant_id` in request bodies are ignored.
- Session reads and writes are owner-checked.
- `POST /api/v1/stream` returns the effective session ID in `X-Session-Id` when sessioning is enabled.

Useful knobs:

```env
GATEWAY_ANONYMOUS__COOKIE_NAME=ag_anon_id
GATEWAY_ANONYMOUS__HEADER_NAME=X-AG-Anonymous-Id
GATEWAY_ANONYMOUS__TTL_DAYS=30
GATEWAY_SESSION__ANONYMOUS_TTL_SECONDS=86400
GATEWAY_SESSION__AUTHENTICATED_TTL_SECONDS=604800
```
