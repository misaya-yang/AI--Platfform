# =============================================================================
# AI Gateway Backend - Multi-stage Dockerfile
# =============================================================================
# Build: docker build -t ai-gateway:latest .
# Run:   docker run -p 8080:8080 --env-file .env ai-gateway:latest
#
# 国内构建（使用镜像源）:
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t ai-gateway:latest .
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Install dependencies
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# Build arguments for mirror configuration
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""

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
# Use mirror if specified via build args
RUN pip install --no-cache-dir --upgrade pip \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}} && \
    pip install --no-cache-dir hatchling \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

# Copy source code and install with all optional dependencies
COPY src/ ./src/
RUN pip install --no-cache-dir ".[all]" \
    --index-url ${PIP_INDEX_URL} \
    ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}}

# Pre-download PaddleOCR models (English + Arabic) to avoid first-run delay
RUN python -c "\
from paddleocr import PaddleOCR; \
PaddleOCR(use_angle_cls=True, lang='en', use_gpu=False, show_log=False); \
PaddleOCR(use_angle_cls=True, lang='ar', use_gpu=False, show_log=False); \
print('PaddleOCR models pre-downloaded successfully')"

# -----------------------------------------------------------------------------
# Stage 2: Runtime - Minimal production image
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime dependencies (including PaddleOCR native libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy pre-downloaded PaddleOCR models from builder
COPY --from=builder /root/.paddleocr /home/appuser/.paddleocr

# Copy application code
COPY src/ ./src/
COPY database/ ./database/
COPY config/ ./config/

# Create necessary directories and fix permissions
RUN mkdir -p /app/logs && \
    chown -R appuser:appuser /app /home/appuser/.paddleocr

# Switch to non-root user
USER appuser

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8080

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
