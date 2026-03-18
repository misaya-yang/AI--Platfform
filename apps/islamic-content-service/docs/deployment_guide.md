# Islamic Content Service 部署指南

## 1. 最简部署（3 步）

```bash
# 1. 准备配置
cd apps/islamic-content-service
cp .env.example .env
# 编辑 .env：改密码、填 Quran 凭证（可选）

# 2. 启动
docker compose -f docker-compose.islamic-content.yml up -d

# 3. 同步 Dua 数据（首次）
docker compose -f docker-compose.islamic-content.yml exec islamic-content \
  python -m islamic_content_service.cli sync bootstrap --sources dua
```

完成后访问 `http://localhost:8091/docs` 查看 Swagger。

## 2. .env 必改项

Docker 部署时，`.env` 中的 DSN 和 Redis 地址需要指向容器名：

```env
ISLAMIC_CONTENT_DATABASE__DSN=postgresql://postgres:111111@postgres:5432/gateway
ISLAMIC_CONTENT_CACHE__REDIS_URL=redis://:111111@redis:6379/1
ISLAMIC_CONTENT_DATABASE__AUTO_MIGRATE=true
```

> 注意：Docker Compose 内部用 `postgres` 和 `redis`（服务名），不是 `127.0.0.1`。

## 3. 数据同步

### Dua（本地数据，无需外部凭证）

```bash
docker compose -f docker-compose.islamic-content.yml exec islamic-content \
  python -m islamic_content_service.cli sync dua
```

### Quran（需要 Quran Foundation 凭证）

在 `.env` 中填写：
```env
ISLAMIC_CONTENT_QURAN__CLIENT_ID=your_client_id
ISLAMIC_CONTENT_QURAN__CLIENT_SECRET=your_client_secret
```

然后：
```bash
docker compose -f docker-compose.islamic-content.yml exec islamic-content \
  python -m islamic_content_service.cli sync quran
```

> Quran 全量同步（含所有翻译和诵读）约需 30-50 分钟。

### Hadith（需要 Sunnah API Key）

在 `.env` 中填写：
```env
ISLAMIC_CONTENT_HADITH__API_KEY=your_sunnah_api_key
```

然后：
```bash
docker compose -f docker-compose.islamic-content.yml exec islamic-content \
  python -m islamic_content_service.cli sync hadith
```

### 一键全量同步

```bash
docker compose -f docker-compose.islamic-content.yml exec islamic-content \
  python -m islamic_content_service.cli sync bootstrap --sources quran,hadith,dua
```

## 4. 验证

```bash
# 健康检查
curl http://localhost:8091/health

# 就绪检查（含数据完整性）
curl http://localhost:8091/health/ready

# 数据统计
curl http://localhost:8091/api/v1/meta/canonical-summary

# Dua 冒烟
curl http://localhost:8091/api/v1/dua/categories
curl http://localhost:8091/api/v1/dua/DUA-0001

# Quran 冒烟（需已同步）
curl http://localhost:8091/api/v1/quran/ayahs/1:1/minimal
```

## 5. 端口说明

| 服务 | 默认端口 | 环境变量 |
|------|----------|----------|
| API 服务 | 8091 | `ISLAMIC_CONTENT_APP__PORT` |
| PostgreSQL | 5433 | `POSTGRES_PORT` |
| Redis | 6381 | `REDIS_PORT` |

## 6. 数据持久化

Docker Compose 使用 named volumes：
- `ic-pg-data`：PostgreSQL 数据
- `ic-redis-data`：Redis 数据

重建容器不会丢数据。清除数据：
```bash
docker compose -f docker-compose.islamic-content.yml down -v
```

## 7. 共享已有 PostgreSQL / Redis

如果已有 PostgreSQL 和 Redis（如 ai-gateway 的基础设施），只需运行 API 服务容器：

```bash
docker build -t islamic-content-service .
docker run -d \
  --name islamic-content-service \
  --env-file .env \
  -p 8091:8091 \
  --network ai-gateway_ai-gateway-net \
  islamic-content-service
```

`.env` 中的 DSN 和 Redis 地址指向已有服务即可。

## 8. 生产建议

- 设置 `ISLAMIC_CONTENT_BOOTSTRAP__ON_START=true` 让服务启动时自动同步 Dua
- PostgreSQL 密码不要用默认的 `111111`
- Redis 密码不要用默认的 `111111`
- 建议用独立 schema（如 `islamic_content`）隔离数据
- 建议配置 Nginx/Traefik 反代，不直接暴露 8091
