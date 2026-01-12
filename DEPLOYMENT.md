# AI Gateway - Deployment Guide

## Quick Start

### 1. Clone and Configure

```bash
# Clone the repository
git clone <repository-url>
cd Agent_Gateway

# Copy environment template
cp .env.production .env

# Edit configuration (IMPORTANT: change passwords and secrets!)
nano .env
```

### 2. Deploy with Docker Compose

```bash
# Make scripts executable
chmod +x scripts/*.sh

# Deploy all services
./scripts/deploy.sh

# Or deploy with fresh builds
./scripts/deploy.sh --build
```

### 3. Access the Application

- **Frontend (Web Console)**: http://localhost:80
- **Backend API**: http://localhost:8080
- **API Documentation**: http://localhost:8080/docs
- **Health Check**: http://localhost:8080/health

---

## Deployment Options

### Full Stack Deployment

Deploy all services (PostgreSQL, Redis, Qdrant, Backend, Frontend):

```bash
docker-compose up -d
```

### Infrastructure Only

Deploy only databases (useful when backend runs separately):

```bash
docker-compose up -d postgres redis qdrant
```

### Application Only

Deploy only application services (requires external databases):

```bash
docker-compose up -d gateway frontend
```

---

## Manual Docker Commands

If you prefer running containers individually (like your original setup):

### PostgreSQL
```bash
docker run -d --name ai-gateway-pg \
  -e POSTGRES_PASSWORD=111111 \
  -e POSTGRES_DB=gateway \
  -p 5432:5432 \
  -v pg-data:/var/lib/postgresql/data \
  --restart unless-stopped \
  postgres:15-alpine
```

### Redis
```bash
docker run -d --name ai-gateway-redis \
  -p 6379:6379 \
  -v redis-data:/data \
  --restart unless-stopped \
  redis:7-alpine redis-server --appendonly yes --requirepass 111111
```

### Qdrant
```bash
docker run -d --name ai-gateway-qdrant \
  -p 6333:6333 \
  -p 6334:6334 \
  -v qdrant-data:/qdrant/storage \
  --restart unless-stopped \
  qdrant/qdrant:latest
```

### Backend
```bash
docker build -t ai-gateway:latest .

# Note: host.docker.internal works on Docker Desktop (Mac/Windows)
# For Linux servers, use one of these alternatives:
#   Option 1: Add --add-host=host.docker.internal:host-gateway (Docker 20.10+)
#   Option 2: Use the actual host IP (e.g., 172.17.0.1 or your server IP)
#   Option 3: Use container names with --network (recommended, see docker-compose.yml)

# For Docker Desktop (Mac/Windows):
docker run -d --name ai-gateway-backend \
  -p 8080:8080 \
  --env-file .env \
  -e GATEWAY_DATABASE__DSN=postgresql://postgres:111111@host.docker.internal:5432/gateway \
  -e GATEWAY_REDIS__URL=redis://:111111@host.docker.internal:6379/0 \
  -e GATEWAY_KNOWLEDGE__QDRANT__URL=http://host.docker.internal:6333 \
  --restart unless-stopped \
  ai-gateway:latest

# For Linux servers (Docker 20.10+):
docker run -d --name ai-gateway-backend \
  -p 8080:8080 \
  --add-host=host.docker.internal:host-gateway \
  --env-file .env \
  -e GATEWAY_DATABASE__DSN=postgresql://postgres:111111@host.docker.internal:5432/gateway \
  -e GATEWAY_REDIS__URL=redis://:111111@host.docker.internal:6379/0 \
  -e GATEWAY_KNOWLEDGE__QDRANT__URL=http://host.docker.internal:6333 \
  --restart unless-stopped \
  ai-gateway:latest
```

### Frontend
```bash
cd web
docker build -t ai-gateway-web:latest .

docker run -d --name ai-gateway-frontend \
  -p 80:80 \
  --restart unless-stopped \
  ai-gateway-web:latest
```

---

## Environment Variables

### Required Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `POSTGRES_PASSWORD` | PostgreSQL password | `111111` |
| `REDIS_PASSWORD` | Redis password | `111111` |
| `JWT_SECRET` | JWT signing secret | (must change!) |

### Optional Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `GATEWAY_PORT` | Backend API port | `8080` |
| `FRONTEND_PORT` | Frontend web port | `80` |
| `LANGGRAPH_ENABLED` | Enable LangGraph | `false` |
| `OPENAI_API_KEY` | OpenAI API key for embeddings | - |
| `DASHSCOPE_API_KEY` | Aliyun DashScope API key | - |

---

## Database Initialization

The database schema is automatically initialized on first startup when `GATEWAY_DATABASE__AUTO_INIT=true`.

To manually initialize:

```bash
# Via script
./scripts/migrate.sh --init

# Via docker exec
docker exec -i ai-gateway-pg psql -U postgres -d gateway < database/schema.sql
```

---

## Backup & Restore

### Create Backup
```bash
./scripts/backup.sh
```

### Restore from Backup
```bash
# Restore from latest backup
./scripts/backup.sh --restore

# Restore from specific file
./scripts/backup.sh --restore backups/gateway_20240101_120000.sql.gz
```

### List Backups
```bash
./scripts/backup.sh --list
```

---

## Monitoring

### View Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f gateway
```

### Check Status
```bash
./scripts/deploy.sh --status
```

### Health Endpoints

- Liveness: `GET /health/live`
- Readiness: `GET /health/ready`
- Metrics: `GET /metrics` (Prometheus format)

---

## Scaling

### Horizontal Scaling

For production deployments with high traffic:

```yaml
# docker-compose.prod.yml
services:
  gateway:
    deploy:
      replicas: 3
```

### Worker Configuration

Adjust worker concurrency based on your server resources:

```bash
TASK_WORKER_CONCURRENCY=4
KNOWLEDGE_WORKER_CONCURRENCY=2
```

---

## Security Checklist

- [ ] Change default passwords (`POSTGRES_PASSWORD`, `REDIS_PASSWORD`)
- [ ] Generate strong JWT secret (`openssl rand -hex 32`)
- [ ] Configure HTTPS with reverse proxy (nginx/traefik)
- [ ] Restrict database ports to internal network
- [ ] Enable rate limiting in production
- [ ] Regular backups scheduled

---

## Troubleshooting

### Container won't start

```bash
# Check logs
docker-compose logs gateway

# Check if ports are in use
netstat -tlnp | grep 8080
```

### Database connection failed

```bash
# Verify PostgreSQL is healthy
docker exec ai-gateway-pg pg_isready -U postgres

# Check network
docker network ls
docker network inspect ai-gateway-network
```

### Frontend can't reach backend

Ensure nginx is configured to proxy to the correct backend host:
- In docker-compose: `gateway:8080`
- Standalone: `host.docker.internal:8080` or actual IP

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Docker Network                          │
│                    (ai-gateway-network)                      │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                │
│  │ Frontend │   │ Backend  │   │ LangGraph│                │
│  │ (nginx)  │──▶│ (FastAPI)│──▶│(optional)│                │
│  │  :80     │   │  :8080   │   │  :2024   │                │
│  └──────────┘   └────┬─────┘   └──────────┘                │
│                      │                                       │
│         ┌────────────┼────────────┐                         │
│         ▼            ▼            ▼                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │PostgreSQL│  │  Redis   │  │  Qdrant  │                  │
│  │  :5432   │  │  :6379   │  │  :6333   │                  │
│  └──────────┘  └──────────┘  └──────────┘                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```
