# Deployment Guide

This guide covers Docker Compose deployment for the standalone AI Gateway project.

For a first local run, start with `README.md`. Use this document when you want a repeatable deployment checklist for a server or shared environment.

## Deployment Model

The default stack contains:

- `postgres`: primary relational database
- `redis`: cache, sessions, and queues
- `qdrant`: vector database for the knowledge base
- `knowledge-service`: KB CRUD, ingestion, indexing, and retrieval
- `assistant-service`: general AI assistant plus bundled Agent Plugin runtime
- `gateway`: public API, auth, proxy, sessions, and readiness
- `frontend`: web console

The gateway and frontend are the public entry points. Assistant service remains private to the Docker network. Infrastructure ports are bound to `127.0.0.1` by default.

## Required Configuration

Create a real `.env` from the example:

```bash
cp .env.example .env
```

Never commit `.env`.
If the real env file is managed outside this repository, pass it to Make with
`ENV_FILE`:

```bash
make validate-config ENV_FILE=/path/to/.env
make validate ENV_FILE=/path/to/.env
make deploy-app ENV_FILE=/path/to/.env ARGS="--no-migrate"
```

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

Use `make validate-config ENV_FILE=/path/to/.env` when the env file is stored
outside the repository.

For repository-only or CI checks that must not depend on private secrets, run:

```bash
make validate-example-config
```

That target validates the committed `.env.example` shape and Docker Compose
rendering. It does not prove a deployment is release-ready; production and
shared environments must still pass `make validate-config` and `make validate`
with a populated real env file.

This checks:

- required secrets are present and not placeholders
- chat and embedding keys are configured
- provider names and ports are valid
- host ports are not duplicated
- bundled plugin paths and data roots are declared
- `VITE_AUTH_EMAIL_DOMAIN` matches `AUTH_ALLOWED_EMAIL_DOMAIN` when set for non-local auth domains
- `VITE_SUPPORT_EMAIL` is blank or uses a real non-example mailbox for non-local auth domains
- explicit frontend API and telemetry runtime URLs use a same-origin path or `https://` and are not localhost or loopback for non-local auth domains
- CORS values are explicit http(s) origin arrays, without wildcards or
  non-https or localhost origins for non-local auth domains
- Docker Compose interpolates successfully

The validator does not print secret values.

## Start

Build, start, migrate, and validate everything:

```bash
make quickstart
```

Lower-level start and migration commands:

```bash
docker compose --env-file .env up -d --build --remove-orphans
bash scripts/new/migrate.sh --env .env --auto
```

Validate the running stack:

```bash
make validate
make status
```

`make status` prints the full health table and exits nonzero if any check is not
available, so it can be used as a release gate instead of only a visual summary.

Validation, status, deploy, migration, backup, stop/restart/log, and development
helper Make targets accept `ENV_FILE=/path/to/.env`; the default is `.env`.
Lower-level deploy, migrate, backup, and setup-dev scripts also accept
`--env FILE` when called directly.

Expected public endpoints:

- Frontend: `http://localhost:8081`
- API docs: `http://localhost:8080/docs`
- Gateway readiness: `http://localhost:8080/health/ready`

## Update

Pull or apply the new source, then rebuild:

```bash
make deploy-build
```

For app-only updates, restart the public entrypoints and application microservices while leaving infrastructure volumes alone:

```bash
make deploy-app
```

For infrastructure-only startup:

```bash
make deploy-infra
```

`deploy-infra` and `deploy-app` are mutually exclusive. Run them as separate commands if both infrastructure and application services need work.
`deploy-app` still runs database migrations by default after PostgreSQL is healthy. Use `make deploy-app ARGS="--no-migrate"` when you only want to restart application services.
Automatic migrations initialize `database/schema.sql` first when the base
schema is missing, then run pending files from `database/migrations`.
Automatic migration failures stop deployment; fix the failed migration before rerunning deploy.
Database restore uses the selected env file and stops on the first SQL error;
restore against shared data requires a current backup and explicit approval.

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

Docgen plugin artifacts use the shared `ai-gateway_gateway-data` volume and are
imported into the Assistant artifact store. Older deployments may still have an
unused `ai-gateway_docgen-artifacts` volume; remove it only after confirming no
legacy files are needed.

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
- Monitor gateway `/health/ready`; it checks DB, Redis, knowledge service, and
  Assistant. Runtime validation also verifies the bundled docgen stdio child
  inside the Assistant container.
- Scrape gateway `/metrics` from your monitoring system and include it in release smoke checks; `gateway_up` should be present after startup.

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
