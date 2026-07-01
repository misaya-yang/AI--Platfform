FROM python:3.12-slim AS builder

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""

# Install workspace member package first (ai-gateway-core). Pip doesn't
# understand uv workspaces so installing from path satisfies the
# ``ai-gateway-core`` entry in the knowledge-service pyproject below.
# Build context is the repo root (docker-compose ``context: ./ai-gateway``)
# so packages/ sits next to apps/.
COPY packages/ /build/packages/
RUN pip install --upgrade pip && \
    pip install --no-cache-dir /build/packages/ai-gateway-core \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

COPY apps/knowledge-service/pyproject.toml apps/knowledge-service/README.md ./
COPY apps/knowledge-service/src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install . \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY apps/knowledge-service/src ./src

EXPOSE 8092
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8092/health || exit 1

CMD ["python", "-m", "uvicorn", "knowledge_service.main:app", "--host", "0.0.0.0", "--port", "8092"]
