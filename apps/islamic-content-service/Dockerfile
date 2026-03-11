FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

EXPOSE 8091

CMD ["python", "-m", "uvicorn", "islamic_content_service.main:app", "--host", "0.0.0.0", "--port", "8091"]
