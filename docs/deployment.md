# AI Gateway Deployment Guide

## Prerequisites

- Python 3.10+
- PostgreSQL 14+
- Redis 7+ (optional, for caching)
- Qdrant (optional, for knowledge base)

## Quick Start

### 1. Clone and Setup

```bash
# Clone the repository
git clone https://github.com/your-org/ai-gateway.git
cd ai-gateway

# Create conda environment (recommended)
conda create -n ai_gateway python=3.12
conda activate ai_gateway

# Install dependencies (full installation)
pip install -e ".[all,dev]"

# Or install specific feature sets:
# pip install -e "."                    # Core only
# pip install -e ".[database]"          # + PostgreSQL, Redis
# pip install -e ".[knowledge]"         # + Qdrant, DashScope
# pip install -e ".[documents]"         # + PDF, DOCX parsing (pypdf, pdfplumber, python-docx)
# pip install -e ".[all]"               # All features
```

### 2. Configuration

```bash
# Copy example configuration
cp .env.example .env

# Edit .env with your settings
# Key settings to configure:
#   - GATEWAY_DATABASE__DSN
#   - GATEWAY_REDIS__URL
#   - GATEWAY_LANGGRAPH__INSTANCE_URLS
```

### 3. Database Setup

```bash
# Initialize database (creates database, runs schema + migrations)
python database/cli.py init

# Or check status first
python database/cli.py status
```

### 4. Start the Backend Server

```bash
# Development mode
uvicorn src.main:app --reload --host 0.0.0.0 --port 8080

# Production mode
uvicorn src.main:app --host 0.0.0.0 --port 8080 --workers 4
```

### 5. Start the Frontend (Optional)

```bash
cd web

# Install dependencies
npm install

# Development mode (http://localhost:3000)
npm run dev

# Production build
npm run build
```

---

## Database Management

### CLI Commands

```bash
# Initialize database (first-time setup)
python database/cli.py init

# Check migration status
python database/cli.py status

# Run pending migrations
python database/cli.py migrate

# Run specific migration
python database/cli.py migrate 003

# Check database connection and tables
python database/cli.py check

# Reset database (CAUTION: drops all data!)
python database/cli.py reset
```

### Database Structure

```
database/
├── cli.py              # Unified database CLI tool
├── schema.sql          # Base schema (v2.0.0, 14 core tables)
└── migrations/
    ├── 002_kbms_enhancements.sql      # Knowledge base enhancements
    └── 003_proxy_enhancements.sql     # Transparent proxy features
```

### Migration Workflow

1. **New Installation**: Run `python database/cli.py init`
2. **Existing Installation**: Run `python database/cli.py migrate`
3. **Adding New Migration**:
   - Create `database/migrations/004_your_feature.sql`
   - Use naming convention: `NNN_description.sql`
   - Run `python database/cli.py migrate`

---

## Environment Configuration

### Required Settings

```bash
# Server
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8080

# Database (PostgreSQL)
GATEWAY_DATABASE__ENABLED=true
GATEWAY_DATABASE__DSN=postgresql://user:password@host:5432/gateway
GATEWAY_DATABASE__AUTO_INIT=true

# Redis (optional but recommended)
GATEWAY_REDIS__ENABLED=true
GATEWAY_REDIS__URL=redis://:password@host:6379/0
```

### LangGraph Configuration

```bash
GATEWAY_LANGGRAPH__ENABLED=true
GATEWAY_LANGGRAPH__INSTANCE_URLS=http://langgraph-server:2024
GATEWAY_LANGGRAPH__AUTH_TOKEN=your-internal-token
```

### Knowledge Base Configuration

```bash
GATEWAY_KNOWLEDGE__ENABLED=true
GATEWAY_KNOWLEDGE__QDRANT__URL=http://qdrant:6333
GATEWAY_KNOWLEDGE__DASHSCOPE__API_KEY=sk-xxx
```

---

## Docker Deployment

### docker-compose.yml

```yaml
version: '3.8'

services:
  gateway:
    build: .
    ports:
      - "8080:8080"
    environment:
      - GATEWAY_DATABASE__DSN=postgresql://postgres:password@postgres:5432/gateway
      - GATEWAY_REDIS__URL=redis://redis:6379/0
    depends_on:
      - postgres
      - redis
    command: >
      sh -c "python database/cli.py init &&
             uvicorn src.main:app --host 0.0.0.0 --port 8080"

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: gateway
      POSTGRES_PASSWORD: password
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    command: redis-server --requirepass password
    volumes:
      - redis_data:/data

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
    volumes:
      - qdrant_data:/qdrant/storage

volumes:
  postgres_data:
  redis_data:
  qdrant_data:
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[all]"

# Copy source code
COPY . .

# Run migrations and start server
CMD ["sh", "-c", "python database/cli.py init && uvicorn src.main:app --host 0.0.0.0 --port 8080"]
```

---

## Health Checks

```bash
# Liveness probe
curl http://localhost:8080/health/live

# Readiness probe (checks DB + Redis)
curl http://localhost:8080/health/ready

# Full health check
curl http://localhost:8080/health
```

---

## Troubleshooting

### Database Connection Failed

```bash
# Check PostgreSQL is running
psql -h localhost -U postgres -c "SELECT 1"

# Verify DSN format
echo $GATEWAY_DATABASE__DSN
# Should be: postgresql://user:password@host:port/database
```

### Redis Connection Failed

```bash
# Check Redis is running
redis-cli -h localhost ping

# Verify URL format
echo $GATEWAY_REDIS__URL
# Should be: redis://:password@host:port/db
```

### Migration Errors

```bash
# Check current status
python database/cli.py status

# View PostgreSQL logs for detailed errors
docker logs postgres_container

# Reset and retry (CAUTION: data loss)
python database/cli.py reset
```

### Knowledge Base Document Upload Failed

If PDF/DOCX uploads fail with parsing errors:

```bash
# Check if document parsing libraries are installed
python -c "import pypdf; import pdfplumber; print('PDF: OK')"
python -c "from docx import Document; print('DOCX: OK')"

# Install missing dependencies
pip install pypdf pdfplumber python-docx beautifulsoup4 lxml

# Or install the documents group
pip install "ai-gateway[documents]"
```

**Required libraries by file type:**
| File Type | Required Package |
|-----------|------------------|
| PDF | `pypdf`, `pdfplumber` |
| DOCX | `python-docx` |
| HTML | `beautifulsoup4`, `lxml` |
| DOC (legacy) | `textract` + system dependencies |

---

## Production Checklist

- [ ] Set strong passwords for all services
- [ ] Configure HTTPS/TLS termination (nginx/traefik)
- [ ] Set up database backups
- [ ] Configure log aggregation
- [ ] Set up monitoring (Prometheus + Grafana)
- [ ] Configure rate limits appropriately
- [ ] Review and restrict CORS settings
- [ ] Enable JWT authentication for production
