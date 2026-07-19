# syntax=docker/dockerfile:1.7
# =============================================================================
# Migration runner — Phase 6 microservice schema split.
# =============================================================================
# A tiny one-shot container. Compose runs it once on `docker compose up`,
# blocking gateway/AS/KS until it exits success. Used to:
#   1. Create per-service schemas (gateway/assistant/knowledge).
#   2. Move tables out of public into their owning schema.
#   3. Run any future per-service migration SQL files.
#
# Why a separate service:
#   - Removes the "service startup auto-migrates" antipattern (race conditions
#     during multi-service rollout, hard to reason about during deploys).
#   - One pod / one job runs migrations; service replicas wait. No more
#     migration_already_applied conditional logic in service code.
#
# This is NOT Alembic — Phase 6 keeps the existing custom migration runner
# style for minimum disruption. Alembic upgrade is a Phase-7 follow-up.
# =============================================================================

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_BASE_IMAGE}

ARG APP_VERSION=2.0.0
ARG VCS_REF=unknown
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""
LABEL org.opencontainers.image.title="AI Gateway Migration Runner" \
      org.opencontainers.image.source="https://github.com/misaya-yang/AI--Platfform" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$VCS_REF"

WORKDIR /app

RUN --mount=type=cache,id=ai-gateway-pip,target=/root/.cache/pip,sharing=locked \
    pip install 'asyncpg==0.31.0' \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

# Copy only what the migrator needs — keep the image tight.
COPY database/migrate_per_service.py /app/database/migrate_per_service.py
COPY database/migrations/per_service /app/database/migrations/per_service

# Same DATABASE_URL fallback chain as the runner expects.
ENV PYTHONUNBUFFERED=1

RUN useradd --uid 1000 --create-home migrator && chown -R migrator:migrator /app
USER migrator

ENTRYPOINT ["python", "/app/database/migrate_per_service.py"]
