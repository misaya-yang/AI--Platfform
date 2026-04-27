# Assistant Service — independent microservice
#
# As of Phase 5f Batch B, AS still bundles gateway's src/ because main.py
# imports a small fixed set of concretes that haven't been moved yet:
#
#   - src.persistence.database          (DatabaseStorage class)
#   - src.services.session.database_session_manager  (DatabaseSessionManager)
#   - src.services.knowledge.kb_proxy_client         (KBProxyClient)
#   - src.services.metrics.realtime_metrics          (get_realtime_metrics)
#   - src.services.metrics.usage_recorder            (get_usage_recorder)
#
# Phase 5f Batch C will move these into ai_gateway_core, after which the
# COPY src/ + pip install ".[all]" lines below can be removed and the
# image will no longer contain /app/src/.
#
# Storage stack (image_storage / artifact_storage / file_storage) was moved
# to ai_gateway_core in Batch B so it does NOT need src/ anymore.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install workspace member package first (ai-gateway-core). Pip doesn't
# understand uv workspaces so installing from path satisfies the
# ``ai-gateway-core`` entry in both pyprojects below.
COPY packages/ ./packages/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ./packages/ai-gateway-core

# Install gateway deps (assistant-service main.py composition root still
# imports kernel concretes from src/: persistence, session, metrics, storage).
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[all]" 2>/dev/null || pip install --no-cache-dir "." && \
    find /opt/venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true

# Install assistant-service deps
COPY apps/assistant-service/pyproject.toml apps/assistant-service/README.md /build/assistant-service/
RUN cd /build/assistant-service && pip install --no-cache-dir "." 2>/dev/null; true

# --- Runtime ---
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy gateway src (compatibility shim)
COPY src/ ./src/

# Copy assistant service
COPY apps/assistant-service/src/ ./apps/assistant-service/src/

RUN useradd -m appuser && mkdir -p /app/logs /app/uploads && chown -R appuser:appuser /app
USER appuser

EXPOSE 8093

# Set PYTHONPATH so both gateway src and assistant_service are importable
ENV PYTHONPATH="/app:/app/apps/assistant-service/src"

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -f http://localhost:8093/health || exit 1

CMD ["uvicorn", "assistant_service.main:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1", "--timeout-keep-alive", "120"]
