# Assistant Service — extracted from AI Gateway monolith
# Shares the same codebase but runs as an independent FastAPI app

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

# Copy the entire gateway source (shares code)
COPY src/ /app/src/
COPY requirements.txt /app/

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy the assistant-service entry point
COPY assistant-service/app.py /app/assistant_app.py

# Non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8093

CMD ["uvicorn", "assistant_app:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "2"]
