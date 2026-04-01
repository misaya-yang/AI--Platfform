# Assistant Service — extracted AI assistant microservice
# Shares code with Gateway but runs independently on port 8093

FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

# Python deps (same as gateway)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy gateway source (shared code) and assistant-service entry point
COPY src/ /app/src/
COPY assistant-service/app.py /app/assistant_app.py

# Non-root
RUN useradd -m appuser && mkdir -p /app/logs && chown -R appuser:appuser /app
USER appuser

EXPOSE 8093

CMD ["uvicorn", "assistant_app:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "1", "--timeout-keep-alive", "120"]
