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

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir 'asyncpg>=0.29'

# Copy only what the migrator needs — keep the image tight.
COPY database/migrate_per_service.py /app/database/migrate_per_service.py
COPY database/migrations/per_service /app/database/migrations/per_service

# Same DATABASE_URL fallback chain as the runner expects.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/database/migrate_per_service.py"]
