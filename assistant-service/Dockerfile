# Assistant Service — shares the gateway's Python packages
# Build: reuse gateway's pyproject.toml for dependencies

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install deps from gateway's pyproject.toml
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir ".[all]" || pip install --no-cache-dir "." && \
    find /opt/venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true

# --- Runtime ---
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY src/ ./src/
COPY assistant-service/app.py ./assistant_app.py

RUN useradd -m appuser && mkdir -p /app/logs && chown -R appuser:appuser /app
USER appuser

EXPOSE 8093

CMD ["uvicorn", "assistant_app:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1", "--timeout-keep-alive", "120"]
