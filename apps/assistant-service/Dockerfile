# Assistant Service — independent microservice
#
# Phase 5f Batch C complete: the AS image no longer bundles gateway's src/.
# All concretes the composition root needs (DatabaseStorage,
# DatabaseSessionManager, KBProxyClient, get_realtime_metrics,
# get_usage_recorder, plus the storage stack from Batch B) live in
# ai_gateway_core, which is installed as a package. The container is now
# truly free of the gateway repo.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install workspace member package (ai-gateway-core). Pip doesn't
# understand uv workspaces so installing from path is what satisfies the
# ``ai-gateway-core`` entry in apps/assistant-service/pyproject.toml.
COPY packages/ ./packages/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ./packages/ai-gateway-core

# Install assistant-service deps. No more pyproject.toml/src/ from the
# gateway repo — the AS pyproject's ai-gateway-core dependency is
# satisfied above, and main.py imports nothing from gateway src.
COPY apps/assistant-service/pyproject.toml apps/assistant-service/README.md /build/assistant-service/
RUN cd /build/assistant-service && pip install --no-cache-dir "." 2>/dev/null; true

# --- Runtime ---
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy assistant service only — no gateway src/ shim required.
COPY apps/assistant-service/src/ ./apps/assistant-service/src/

RUN useradd -m appuser && mkdir -p /app/logs /app/uploads && chown -R appuser:appuser /app
USER appuser

EXPOSE 8093

# PYTHONPATH only needs assistant_service; ai_gateway_core is installed
# into site-packages by the builder stage above.
ENV PYTHONPATH="/app/apps/assistant-service/src"

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -f http://localhost:8093/health || exit 1

CMD ["uvicorn", "assistant_service.main:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1", "--timeout-keep-alive", "120"]
