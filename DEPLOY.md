# Deployment Guide

This guide covers Docker Compose deployment for the standalone AI Gateway project.

For a first local run, start with `README.md`. Use this document when you want a repeatable deployment checklist for a server or shared environment.

## Deployment Model

The default stack contains:

- `postgres`: primary relational database
- `redis`: cache, sessions, and queues
- `qdrant`: vector database for the knowledge base
- `knowledge-service`: KB CRUD, ingestion, indexing, and retrieval
- `assistant-service`: general AI assistant runtime
- `mcp-docgen-server`: document-generation MCP tool server
- `gateway`: public API, auth, proxy, sessions, and readiness
- `frontend`: web console

The gateway and frontend are the public entry points. Assistant service remains private to the Docker network. Infrastructure ports are bound to `127.0.0.1` by default.

## Required Configuration

Create a real `.env` from the example:

```bash
cp .env.example .env
```

Never commit `.env`.

Fill these required values:

- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `JWT_SECRET`
- `GATEWAY_ASSISTANT_SHARED_SECRET`
- At least one chat provider key
- `KB_EMBEDDING_PROVIDER`
- `KB_EMBEDDING_API_KEY`
- `KB_EMBEDDING_MODEL`
- `KB_EMBEDDING_DIMENSION`

Generate local secrets with:

```bash
openssl rand -hex 32
```

## Preflight

Run configuration validation before building:

```bash
make validate-config
```

This checks:

- required secrets are present and not placeholders
- chat and embedding keys are configured
- provider names and ports are valid
- host ports are not duplicated
- CORS values are JSON arrays
- Docker Compose interpolates successfully

The validator does not print secret values.

## Start

Build and start everything:

```bash
make quickstart
```

Equivalent lower-level command:

```bash
docker compose --env-file .env up -d --build --remove-orphans
```

Validate the running stack:

```bash
make validate
make status
```

Expected public endpoints:

- Frontend: `http://localhost:8081`
- API docs: `http://localhost:8080/docs`
- Gateway readiness: `http://localhost:8080/health/ready`

## Update

Pull or apply the new source, then rebuild:

```bash
make deploy-build
```

For app-only updates:

```bash
make deploy-app
```

For infrastructure-only startup:

```bash
make deploy-infra
```

## Stop and Reset

Stop without deleting data:

```bash
make stop
```

Destructive local reset:

```bash
docker compose down -v --remove-orphans
```

Use the destructive reset only when you intentionally want to delete local PostgreSQL, Redis, Qdrant, KB, and artifact volumes.

## Data Volumes

Compose uses the fixed project name `ai-gateway`, so volume names are stable:

- `ai-gateway_pg-data`
- `ai-gateway_redis-data`
- `ai-gateway_qdrant-data`
- `ai-gateway_gateway-data`
- `ai-gateway_kb-data`
- `ai-gateway_docgen-artifacts`

PostgreSQL initializes its password when the volume is first created. If you change `POSTGRES_PASSWORD` after first boot without resetting the volume, authentication can fail. For local development, reset volumes intentionally with `docker compose down -v --remove-orphans`.

Qdrant collections are tied to embedding dimensions. If you change `KB_EMBEDDING_DIMENSION` or switch providers for an existing dataset, create new collections/datasets or reset Qdrant in local development.

## Production Notes

For production-like deployments:

- Put a reverse proxy or load balancer in front of gateway/frontend.
- Keep PostgreSQL, Redis, and Qdrant ports private.
- Use strong generated values for all secrets.
- Store `.env` in your secret-management system, not in Git.
- Configure explicit CORS origins for your frontend domain.
- Use S3-compatible storage by setting `GATEWAY_STORAGE__BACKEND=s3` and filling the S3 variables.
- Monitor `/health/ready`; it checks DB, Redis, knowledge service, assistant service, and docgen.

## Troubleshooting

Config validation:

```bash
make validate-config
```

Runtime validation:

```bash
make validate
```

Service state:

```bash
make status
docker compose --env-file .env ps
```

Logs:

```bash
docker compose --env-file .env logs --tail=200 gateway
docker compose --env-file .env logs --tail=200 knowledge-service
docker compose --env-file .env logs --tail=200 assistant-service
docker compose --env-file .env logs --tail=200 qdrant
```

Common causes:

- `.env` still contains `change_me_*` placeholders.
- A chat model key is configured, but `KB_EMBEDDING_API_KEY` is missing.
- Redis or PostgreSQL password was changed after volumes were initialized.
- Qdrant contains vectors from a different embedding dimension.
- A host port is already occupied.
