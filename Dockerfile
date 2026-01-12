# =============================================================================
# AI Gateway Backend - Multi-stage Dockerfile
# =============================================================================
# Build: docker build -t ai-gateway:latest .
# Run:   docker run -p 8080:8080 --env-file .env ai-gateway:latest
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Install dependencies
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy project metadata files needed for installation
# Note: README.md is required by pyproject.toml for hatchling build
COPY pyproject.toml README.md ./

# Install dependencies first (better layer caching)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir hatchling

# Copy source code and install with all optional dependencies
COPY src/ ./src/
RUN pip install --no-cache-dir ".[all]"

# -----------------------------------------------------------------------------
# Stage 2: Runtime - Minimal production image
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY src/ ./src/
COPY database/ ./database/
COPY config/ ./config/

# Create necessary directories
RUN mkdir -p /app/logs /app/uploads /app/data/images && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8080 \
    FILE_STORAGE_PATH=/app/uploads

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
