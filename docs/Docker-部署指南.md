# AI Gateway — 纯 Docker 部署指南

> 本文档覆盖 AI Gateway 三大微服务（网关 + 知识库 + Islamic Content API）的完整 Docker 部署流程。

---

## 目录

1. [系统要求](#1-系统要求)
2. [架构总览](#2-架构总览)
3. [快速启动（30 秒）](#3-快速启动30-秒)
4. [自定义配置部署](#4-自定义配置部署)
5. [各服务详细说明](#5-各服务详细说明)
6. [环境变量完整参考](#6-环境变量完整参考)
7. [数据隔离与存储](#7-数据隔离与存储)
8. [LangGraph Agent 集成](#8-langgraph-agent-集成)
9. [运维操作手册](#9-运维操作手册)
10. [监控部署（可选）](#10-监控部署可选)
11. [生产环境加固](#11-生产环境加固)
12. [故障排查](#12-故障排查)

---

## 1. 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| Docker | 24+ | 最新稳定版 |
| Docker Compose | v2+ | v2.20+ |
| CPU | 2 核 | 4 核+ |
| 内存 | 4 GB | 8 GB+ |
| 磁盘 | 20 GB | 50 GB+（含向量数据库） |

---

## 2. 架构总览

### 2.1 服务拓扑

```
                        ┌──────────────────────┐
       :80  ────────────┤  Frontend (Nginx)    │
                        │  React SPA           │
                        └──────────┬───────────┘
                                   │ /api, /v1, /ws
                        ┌──────────▼───────────┐
       :8080 ───────────┤     Gateway          │ ← 主网关（LLM 代理 / 会话 / 知识库路由）
                        └──┬─────┬─────┬───┬───┘
                           │     │     │   │
              ┌────────────▼┐ ┌──▼───┐ ┌▼──▼───────┐
              │ PostgreSQL  │ │Redis │ │  Qdrant   │
              │   :5432     │ │:6379 │ │:6333/6334 │
              └──────┬──────┘ └──┬───┘ └─────┬─────┘
                     │           │           │
         ┌───────────▼───────────▼───────────▼─────────────┐
         │                                                 │
  ┌──────▼──────────────┐              ┌───────────────────▼──┐
  │ Knowledge Service   │              │ Islamic Content      │
  │     :8092           │              │     :8091            │
  │ (KB CRUD + 检索)     │              │ (Quran/Hadith/Dua)  │
  └─────────────────────┘              └──────────────────────┘
```

### 2.2 容器清单（7 个）

| 容器名 | 镜像 | 端口 | 角色 |
|--------|------|------|------|
| `ai-gateway-pg` | postgres:16-alpine | 5432 | 主数据库 |
| `ai-gateway-redis` | redis:7-alpine | 6379 | 缓存 & 会话存储 |
| `ai-gateway-qdrant` | qdrant/qdrant:latest | 6333, 6334 | 向量数据库 |
| `ai-gateway-backend` | ai-gateway:latest | 8080 | API 网关 |
| `ai-gateway-frontend` | ai-gateway-web:latest | 80 | 前端 Web 控制台 |
| `ai-gateway-knowledge` | knowledge-service:latest | 8092 | 知识库微服务 |
| `islamic-content-service` | islamic-content-service:latest | 8091 | Islamic 内容 API |

### 2.3 网络

所有容器运行在同一个 Docker bridge 网络 `ai-gateway-network` 中，服务间通过容器名（DNS）直接通信。

---

## 3. 快速启动（30 秒）

```bash
# 克隆代码
git clone <repo-url> && cd ai-gateway

# 方式一：一键启动（零配置，使用内置默认值）
make quickstart

# 方式二：直接 docker compose
docker compose up -d
```

**不需要**手动创建 `.env` 文件 — 所有默认值已内置在 `docker-compose.yml` 中。

启动后验证：

```bash
# 查看所有容器状态
docker compose ps

# 健康检查
curl http://localhost:8080/health    # Gateway
curl http://localhost:8092/health    # Knowledge Service
curl http://localhost:8091/health    # Islamic Content

# 访问前端
open http://localhost:80
```

| 服务 | 访问地址 |
|------|---------|
| 前端界面 | http://localhost:80 |
| API 网关 | http://localhost:8080 |
| 知识库服务 | http://localhost:8092 |
| Islamic 内容 | http://localhost:8091 |

---

## 4. 自定义配置部署

### 4.1 创建配置文件

```bash
# 从模板创建 .env
cp .env.example .env

# 编辑配置
vim .env
```

### 4.2 生产环境必须修改的配置

| 变量 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| `POSTGRES_PASSWORD` | 数据库密码 | `111111` | `MyStr0ngP@ss!` |
| `REDIS_PASSWORD` | Redis 密码 | `111111` | `RedisP@ss123` |
| `JWT_SECRET` | JWT 签名密钥 | `change-me-...` | 至少 32 位随机字符串 |

生成强密码：

```bash
# 生成 JWT Secret
openssl rand -base64 32

# 生成数据库密码
openssl rand -hex 16
```

### 4.3 启动

```bash
# 使用 make（推荐，含迁移和健康检查）
make deploy

# 或直接 docker compose（重新构建镜像）
docker compose up -d --build
```

### 4.4 构建相关

```bash
# 强制重新构建所有镜像
make deploy-build

# 国内环境使用镜像源构建
make deploy-cn

# 仅启动基础设施（PostgreSQL / Redis / Qdrant）
make deploy-infra

# 仅启动应用层（Gateway / Frontend / 微服务）
make deploy-app
```

---

## 5. 各服务详细说明

### 5.1 Gateway（API 网关）

**端口：** 8080 | **镜像：** `ai-gateway:latest` | **Dockerfile：** `/Dockerfile`

**职责：**
- 统一 API 入口，OpenAI 兼容接口 (`/v1/chat/completions`)
- 多 LLM 供应商透明代理（OpenAI、Claude、Gemini、DeepSeek 等）
- 会话管理、JWT 认证、匿名身份、速率限制
- 知识库集成（路由到 Knowledge Service）
- LangGraph Agent 代理
- 文件上传 & 对象存储（S3/OSS）

**依赖：** PostgreSQL (healthy), Redis (healthy), Qdrant (started)

**技术栈：** Python 3.12 + FastAPI + Uvicorn

**构建细节：**
- 多阶段构建，最终镜像基于 `python:3.12-slim`
- 预下载 PaddleOCR 英文 + 阿拉伯文模型
- 非 root 用户运行（appuser）

```bash
# 单独构建
docker build -t ai-gateway:latest .

# 国内镜像源构建
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t ai-gateway:latest .

# 查看日志
docker compose logs -f gateway
```

### 5.2 Knowledge Service（知识库微服务）

**端口：** 8092 | **镜像：** `knowledge-service:latest` | **Dockerfile：** `/apps/knowledge-service/Dockerfile`

**职责：**
- 文档上传 & 处理（PDF、DOCX、TXT 等）
- 大文件自动分割（>20MB PDF → N 份 500 页）
- OCR 识别（英文 + 阿拉伯文，Tesseract + PaddleOCR）
- 文本分块（层级分块：L2=8KB, L3=2KB）
- 文本嵌入（Gemini / DashScope / SiliconFlow）
- 向量存储 & 检索（Qdrant，支持混合搜索 + RRF 排序）
- 重排序 & 引文追踪
- 检索缓存（Redis）

**依赖：** PostgreSQL (healthy), Redis (healthy), Qdrant (started)

**技术栈：** Python 3.12 + FastAPI + sentence-transformers + Tesseract OCR

**关键环境变量：**

| 变量 | 必填 | 说明 |
|------|------|------|
| `KB_EMBEDDING_PROVIDER` | 否 | 嵌入供应商：`gemini`（默认）或 `dashscope` |
| `KB_EMBEDDING_API_KEY` | 是 | 嵌入 API 密钥（Gemini 或 DashScope） |

```bash
# 单独构建
docker build -t knowledge-service:latest ./apps/knowledge-service/

# 单独启动
docker compose up -d knowledge-service

# 测试检索
curl -X POST http://localhost:8092/api/v1/knowledge/islamic-knowledge/retrieve \
  -H "Content-Type: application/json" \
  -H "X-User-Id: admin" -H "X-Tenant-Id: default" \
  -d '{"query": "what is zakat", "top_k": 5}'
```

### 5.3 Islamic Content Service（Islamic 内容 API）

**端口：** 8091 | **镜像：** `islamic-content-service:latest` | **Dockerfile：** `/apps/islamic-content-service/Dockerfile`

**职责：**
- Quran 经文检索（Quran.foundation API，支持多语言翻译 + 朗诵音频）
- Hadith 圣训检索（Sunnah.com API，Bukhari/Muslim/Abu Dawood）
- Dua 祈祷词（本地 CSV 数据集）
- Redis 缓存（多级 TTL）
- 启动时自动数据库迁移 & 数据引导

**依赖：** PostgreSQL (healthy), Redis (healthy)

**技术栈：** Python 3.12 + FastAPI + asyncpg + httpx

**可选外部凭证：**

| 变量 | 说明 |
|------|------|
| `ISLAMIC_CONTENT_QURAN__CLIENT_ID` | Quran.foundation OAuth2 客户端 ID |
| `ISLAMIC_CONTENT_QURAN__CLIENT_SECRET` | Quran.foundation OAuth2 密钥 |
| `ISLAMIC_CONTENT_HADITH__API_KEY` | Sunnah.com API 密钥 |

```bash
# 单独构建
docker build -t islamic-content-service:latest ./apps/islamic-content-service/

# 数据同步
docker compose exec islamic-content python -m islamic_content_service.cli sync bootstrap --sources quran,dua,hadith

# 仅同步 Dua（无需外部凭证）
docker compose exec islamic-content python -m islamic_content_service.cli sync bootstrap --sources dua
```

### 5.4 Frontend（前端 Web 控制台）

**端口：** 80 | **镜像：** `ai-gateway-web:latest` | **Dockerfile：** `/web/Dockerfile`

**职责：**
- React SPA 前端界面
- Nginx 反向代理，将 `/api/`、`/v1/`、`/ws/` 路由到 Gateway
- 静态资源 Gzip 压缩 & 1 年缓存
- WebSocket 支持

**Nginx 路由规则：**

| 路径 | 后端 | 说明 |
|------|------|------|
| `/api/*` | gateway:8080 | REST API |
| `/v1/*` | gateway:8080 | OpenAI 兼容 API |
| `/ws/*` | gateway:8080 | WebSocket |
| `/metrics` | gateway:8080 | Prometheus 指标 |
| `/health` | 本地 200 | 前端健康检查 |
| `/*` | 本地静态 | SPA fallback |

**技术栈：** React + Vite + pnpm → Nginx Alpine

```bash
# 单独构建
docker build -t ai-gateway-web:latest ./web/

# 国内镜像源构建
docker build --build-arg NPM_REGISTRY=https://registry.npmmirror.com -t ai-gateway-web:latest ./web/
```

---

## 6. 环境变量完整参考

通过项目根目录 `.env` 文件配置，或直接在 `docker compose` 中传入。

### 6.1 基础设施

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `POSTGRES_USER` | `postgres` | PostgreSQL 用户名 |
| `POSTGRES_PASSWORD` | `111111` | PostgreSQL 密码 |
| `POSTGRES_DB` | `gateway` | 数据库名 |
| `POSTGRES_PORT` | `5432` | 宿主机映射端口 |
| `REDIS_PASSWORD` | `111111` | Redis 密码 |
| `REDIS_PORT` | `6379` | 宿主机映射端口 |
| `QDRANT_HTTP_PORT` | `6333` | Qdrant HTTP 端口 |
| `QDRANT_GRPC_PORT` | `6334` | Qdrant gRPC 端口 |

### 6.2 Gateway

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GATEWAY_PORT` | `8080` | 网关宿主机端口 |
| `JWT_SECRET` | `change-me-...` | JWT 签名密钥（生产必改） |
| `TASK_WORKER_CONCURRENCY` | `2` | 后台任务并发数 |
| `LANGGRAPH_ENABLED` | `false` | 是否启用 LangGraph Agent |
| `LANGGRAPH_INSTANCE_URLS` | _(空)_ | Agent 实例 URL（逗号分隔） |
| `LANGGRAPH_AUTH_TOKEN` | _(空)_ | Agent 认证 Token |
| `KNOWLEDGE_WORKER_CONCURRENCY` | `2` | 知识库处理并发数 |
| `DASHSCOPE_API_KEY` | _(空)_ | DashScope 嵌入 API Key |
| `PROXY_BILLING_ENABLED` | `false` | 是否启用代理计费 |
| `STORAGE_BACKEND` | `s3` | 存储后端：`s3` / `oss` / `local` |
| `S3_BUCKET` | _(空)_ | S3 存储桶名 |
| `S3_REGION` | `us-east-1` | S3 区域 |
| `S3_ACCESS_KEY` | _(空)_ | S3 Access Key |
| `S3_SECRET_KEY` | _(空)_ | S3 Secret Key |
| `S3_ENDPOINT_URL` | _(空)_ | S3 兼容端点（MinIO 等） |
| `STORAGE_KEY_PREFIX` | `dev` | 存储路径前缀（环境隔离） |
| `MAX_FILE_SIZE_MB` | `50` | 最大上传文件大小 |

### 6.3 Knowledge Service

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KNOWLEDGE_SERVICE_PORT` | `8092` | 宿主机映射端口 |
| `KB_EMBEDDING_PROVIDER` | `gemini` | 嵌入供应商 |
| `KB_EMBEDDING_API_KEY` | _(空)_ | 嵌入 API Key（**必填**） |
| `KB_WORKER_CONCURRENCY` | `2` | 文档处理并发数 |

> 知识库服务的 S3 存储配置自动继承 Gateway 的 `GATEWAY_STORAGE__*` 变量。

### 6.4 Islamic Content Service

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ISLAMIC_CONTENT_PORT` | `8091` | 宿主机映射端口 |
| `ISLAMIC_CONTENT_SCHEMA` | `islamic_content` | PostgreSQL schema 名 |

### 6.5 Frontend

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FRONTEND_PORT` | `80` | 前端宿主机端口 |

### 6.6 镜像版本

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TAG` | `latest` | 所有服务镜像 tag |

---

## 7. 数据隔离与存储

### 7.1 数据库隔离

所有服务共享同一个 PostgreSQL 实例，但通过不同的 database/schema 隔离数据：

| 服务 | 数据库 | Schema | Redis DB |
|------|--------|--------|----------|
| Gateway | `gateway` | public | db 0 |
| Islamic Content | `gateway` | `islamic_content` | db 1 |
| Knowledge Service | `gateway` | public (kb tables) | db 2 |

### 7.2 数据卷

| Docker Volume | 容器路径 | 用途 |
|---------------|---------|------|
| `ai-gateway-pg-data` | `/var/lib/postgresql/data` | PostgreSQL 数据 |
| `ai-gateway-redis-data` | `/data` | Redis AOF 持久化 |
| `ai-gateway-qdrant-data` | `/qdrant/storage` | 向量数据库 |
| `ai-gateway-logs` | `/app/logs` | Gateway 日志 |
| `ai-gateway-kb-data` | `/app/data` | 知识库本地文件 |

### 7.3 备份与恢复

```bash
# 数据库备份
make backup
# 或手动
docker compose exec postgres pg_dump -U postgres gateway > backup_$(date +%Y%m%d_%H%M%S).sql

# 恢复
make restore
# 或手动
cat backup.sql | docker compose exec -T postgres psql -U postgres gateway

# 列出所有备份
make backup-list

# 备份向量数据库（快照）
docker compose exec qdrant curl -X POST http://localhost:6333/snapshots

# 备份 Redis
docker compose exec redis redis-cli -a 111111 BGSAVE
```

---

## 8. LangGraph Agent 集成

Gateway 支持代理 LangGraph Agent 请求。Agent 作为独立容器部署，Gateway 统一暴露 API。

### 8.1 架构

```
  用户 ──→ Gateway (:8080)
              │
              ├──→ Imam Agent (:8000)
              └──→ Customer Agent (:8000)
```

### 8.2 启用 Agent

1. 在 `langgraph_project` 仓库构建 Agent 镜像：

```bash
cd langgraph_project/agents/Imam_agent
langgraph build -t imam-agent:latest
```

2. 在 `.env` 中启用：

```bash
LANGGRAPH_ENABLED=true
LANGGRAPH_INSTANCE_URLS=http://imam-agent:8000,http://customer-agent:8000
LANGGRAPH_AUTH_TOKEN=your-internal-token
```

3. 将 Agent 加入 `docker-compose.override.yml`：

```yaml
services:
  imam-agent:
    image: imam-agent:latest
    environment:
      LANGSERVE_POSTGRES_URI: "postgresql://postgres:111111@postgres:5432/langgraph_imam"
      REDIS_URI: "redis://:111111@redis:6379/2"
    networks:
      - ai-gateway-net
    depends_on:
      postgres:
        condition: service_healthy

networks:
  ai-gateway-net:
    external: true
    name: ai-gateway-network
```

4. 启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

### 8.3 Agent 数据隔离

| Agent | PostgreSQL DB | Redis DB |
|-------|---------------|----------|
| Imam Agent | `langgraph_imam` | db 2 |
| Customer Agent | `langgraph_customer` | db 3 |
| 新 Agent... | `langgraph_<name>` | db 4+ |

---

## 9. 运维操作手册

### 9.1 Make 快捷命令

| 命令 | 说明 |
|------|------|
| `make quickstart` | 零配置一键部署 |
| `make deploy` | 完整部署（构建+启动+迁移+健康检查） |
| `make deploy-build` | 强制重新构建镜像并部署 |
| `make deploy-cn` | 使用国内镜像源构建部署 |
| `make status` | 查看所有服务状态和健康检查 |
| `make logs` | 实时查看所有日志 |
| `make stop` | 停止所有服务 |
| `make restart` | 重启所有服务 |
| `make migrate` | 运行数据库迁移 |
| `make migrate-status` | 查看迁移状态 |
| `make backup` | 创建数据库备份 |
| `make restore` | 恢复最新备份 |

### 9.2 常用 Docker Compose 命令

```bash
# 查看所有容器状态
docker compose ps

# 查看特定服务日志
docker compose logs -f gateway
docker compose logs -f knowledge-service
docker compose logs -f islamic-content

# 重启单个服务
docker compose restart gateway

# 进入容器 shell
docker compose exec gateway bash
docker compose exec postgres psql -U postgres gateway

# 仅启动基础设施
docker compose up -d postgres redis qdrant

# 资源使用情况
docker stats

# 清理并重新开始（⚠️ 删除所有数据）
docker compose down -v
docker compose up -d --build
```

### 9.3 Islamic Content 数据同步

```bash
# 全量同步
make ic-sync

# 仅同步 Dua（无需 API 凭证）
make ic-sync-dua

# 仅同步 Quran（需要 OAuth2 凭证）
make ic-sync-quran

# 查看同步日志
make ic-logs
```

### 9.4 镜像版本管理

```bash
# 使用指定版本 tag 部署
TAG=v1.2.0 docker compose up -d

# 仅更新 Gateway 镜像
docker compose build gateway
docker compose up -d gateway

# 仅更新 Knowledge Service
docker compose build knowledge-service
docker compose up -d knowledge-service
```

---

## 10. 监控部署（可选）

项目内置企业级监控栈，包含 Prometheus + Grafana + Jaeger + AlertManager。

```bash
# 启动监控栈
docker compose -f docker/monitoring/docker-compose.monitoring.yml up -d
```

| 服务 | 端口 | 用途 |
|------|------|------|
| Prometheus | 9090 | 指标采集 |
| Grafana | 3001 | 可视化面板 |
| Jaeger | 16686 | 分布式链路追踪 |
| AlertManager | 9093 | 告警路由 |

监控指标通过 OpenTelemetry Collector 统一收集，包括：
- HTTP 请求延迟 & 吞吐量
- 数据库连接池状态
- Redis 缓存命中率
- LLM API 调用指标
- 文档处理耗时

---

## 11. 生产环境加固

### 11.1 安全 Checklist

- [ ] 修改所有默认密码（`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET`）
- [ ] 关闭不需要暴露的端口（PostgreSQL 5432、Redis 6379 仅在内网访问时可去掉宿主机映射）
- [ ] 配置 HTTPS（在 Nginx 或外部负载均衡器上终止 TLS）
- [ ] 启用 API Key 认证（`GATEWAY_AUTHENTICATION__API_KEY__ENABLED=true`）
- [ ] 设置速率限制（`GATEWAY_RATE_LIMITS__ENABLED=true`）
- [ ] 配置对象存储（S3/OSS）替代本地文件存储

### 11.2 关闭非必要端口映射

生产环境中，基础设施端口不应暴露到宿主机。创建 `docker-compose.override.yml`：

```yaml
services:
  postgres:
    ports: []
  redis:
    ports: []
  qdrant:
    ports: []
  knowledge-service:
    ports: []
  islamic-content:
    ports: []
```

只保留 Gateway (:8080) 和 Frontend (:80) 对外暴露。

### 11.3 资源限制

```yaml
# docker-compose.override.yml
services:
  gateway:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 4G
        reservations:
          cpus: '1'
          memory: 1G
  knowledge-service:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
  islamic-content:
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 512M
```

### 11.4 日志管理

```yaml
# docker-compose.override.yml 中为所有服务添加日志限制
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "50m"
    max-file: "5"

services:
  gateway:
    logging: *default-logging
  knowledge-service:
    logging: *default-logging
  islamic-content:
    logging: *default-logging
```

---

## 12. 故障排查

### 12.1 容器无法启动

```bash
# 查看容器退出日志
docker compose logs gateway
docker compose logs knowledge-service

# 查看容器详情
docker inspect ai-gateway-backend | jq '.[0].State'

# 常见原因：
# 1. 数据库未就绪 → 等待 PostgreSQL 健康检查通过
# 2. 端口冲突 → lsof -i :8080
# 3. 镜像构建失败 → docker compose build --no-cache gateway
```

### 12.2 数据库连接错误

```bash
# 检查 PostgreSQL 容器
docker compose ps postgres
docker compose logs postgres

# 手动测试连接
docker compose exec postgres psql -U postgres -d gateway -c "SELECT 1;"

# 检查连接字符串
docker compose exec gateway env | grep DATABASE
```

### 12.3 知识库检索异常

```bash
# 检查 Qdrant 状态
curl http://localhost:6333/dashboard

# 检查嵌入 API Key 是否配置
docker compose exec knowledge-service env | grep EMBEDDING

# 查看知识库服务日志
docker compose logs -f knowledge-service
```

### 12.4 Islamic Content 同步失败

```bash
# 检查服务状态
docker compose logs -f islamic-content

# 验证数据库 schema
docker compose exec postgres psql -U postgres -d gateway -c "\dn"

# 手动触发同步（仅 Dua，无需外部 API）
docker compose exec islamic-content python -m islamic_content_service.cli sync bootstrap --sources dua
```

### 12.5 资源不足

```bash
# 查看容器资源使用
docker stats

# 清理 Docker 缓存
docker system prune -f
docker builder prune -f

# 常见内存不足场景：
# - Qdrant 向量数据过大 → 增加宿主机内存或限制集合数量
# - Knowledge Service OCR 处理 → 减小 KB_WORKER_CONCURRENCY
# - Gateway 并发请求 → 减小 TASK_WORKER_CONCURRENCY
```

### 12.6 健康检查调试

```bash
# 查看各容器健康状态
docker compose ps --format "table {{.Name}}\t{{.Status}}"

# 手动执行健康检查
docker exec ai-gateway-backend curl -f http://localhost:8080/health
docker exec ai-gateway-knowledge curl -f http://localhost:8092/health
docker exec islamic-content-service python -c "import urllib.request; urllib.request.urlopen('http://localhost:8091/health')"
```

---

## 附录：端口速查表

| 端口 | 服务 | 协议 | 说明 |
|------|------|------|------|
| 80 | Frontend | HTTP | Web 控制台 |
| 8080 | Gateway | HTTP | REST / OpenAI 兼容 API |
| 8091 | Islamic Content | HTTP | Quran / Hadith / Dua API |
| 8092 | Knowledge Service | HTTP | 知识库 CRUD & 检索 |
| 5432 | PostgreSQL | TCP | 数据库 |
| 6379 | Redis | TCP | 缓存 |
| 6333 | Qdrant | HTTP | 向量数据库 REST |
| 6334 | Qdrant | gRPC | 向量数据库 gRPC |
