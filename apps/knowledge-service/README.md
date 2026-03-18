# Knowledge Base Microservice

Independent KB service extracted from AI Gateway. Provides document ingestion,
chunking, embedding, vector storage (Qdrant), and retrieval APIs.

## Quick Start

```bash
# Install in development mode
pip install -e ".[dev]"

# Run locally
KNOWLEDGE_QDRANT__URL=http://localhost:6333 \
KNOWLEDGE_DATABASE__DSN=postgresql://localhost:5432/gateway \
uvicorn knowledge_service.main:app --reload --port 8092
```

## Configuration

All settings use the `KNOWLEDGE_` prefix with `__` as the nested delimiter.
See `.env.example` for the full list.

## Docker

```bash
docker build -t knowledge-service .
docker run -p 8092:8092 --env-file .env knowledge-service
```
