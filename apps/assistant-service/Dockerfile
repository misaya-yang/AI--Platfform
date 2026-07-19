# syntax=docker/dockerfile:1.7
# Assistant Service — independent microservice
#
# Phase 5f Batch C complete: the AS image no longer bundles gateway's src/.
# All concretes the composition root needs (DatabaseStorage,
# DatabaseSessionManager, KBProxyClient, get_realtime_metrics,
# get_usage_recorder, plus the storage stack from Batch B) live in
# ai_gateway_core, which is installed as a package. The container is now
# truly free of the gateway repo.

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_BASE_IMAGE} AS builder

ARG DEBIAN_MIRROR=https://deb.debian.org

RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources && \
    apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""

WORKDIR /build

# Install workspace member package (ai-gateway-core). Pip doesn't
# understand uv workspaces so installing from path is what satisfies the
# ``ai-gateway-core`` entry in apps/assistant-service/pyproject.toml.
COPY packages/ai-gateway-core/ ./packages/ai-gateway-core/
RUN --mount=type=cache,id=ai-gateway-pip,target=/root/.cache/pip,sharing=locked \
    pip install --upgrade pip && \
    pip install ./packages/ai-gateway-core \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

# Install assistant-service deps. No more pyproject.toml/src/ from the
# gateway repo — the AS pyproject's ai-gateway-core dependency is
# satisfied above, and main.py imports nothing from gateway src.
COPY apps/assistant-service/pyproject.toml apps/assistant-service/README.md /build/assistant-service/
RUN --mount=type=cache,id=ai-gateway-pip,target=/root/.cache/pip,sharing=locked \
    cd /build/assistant-service && \
    pip install "." \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

# --- Runtime ---
FROM ${PYTHON_BASE_IMAGE} AS runtime

ARG APP_VERSION=2.0.0
ARG VCS_REF=unknown
ARG DEBIAN_MIRROR=https://deb.debian.org
LABEL org.opencontainers.image.title="AI Gateway Assistant Service" \
      org.opencontainers.image.source="https://github.com/misaya-yang/AI--Platfform" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$VCS_REF"

WORKDIR /app

RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources && \
    apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy assistant service only — no gateway src/ shim required.
COPY apps/assistant-service/src/ ./apps/assistant-service/src/

RUN useradd --uid 1000 --create-home appuser && \
    mkdir -p /app/logs /app/uploads && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8093

# PYTHONPATH only needs assistant_service; ai_gateway_core is installed
# into site-packages by the builder stage above.
ENV PYTHONPATH="/app/apps/assistant-service/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -fsS http://localhost:8093/health || exit 1

CMD ["uvicorn", "assistant_service.main:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1", "--timeout-keep-alive", "120"]
