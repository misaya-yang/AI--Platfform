# 透明代理模式使用指南

## 概述

透明代理模式允许 AI Gateway 将请求直接转发到 LangGraph 或其他 LLM 服务，支持：

- 🔄 **动态路由转发**：通配符路由 `/api/v1/proxy/{service_name}/{path:path}`
- 📡 **完美流式传输**：自动识别 SSE 响应并流式转发
- 🔐 **可插拔中间件**：鉴权、限流、上下文注入
- 💰 **流式计费**：解析 `event: metadata` 提取 token 使用量
- ❌ **错误透传**：上游 4xx/5xx 错误原样返回

## 快速开始

### 1. 注册 LangGraph 服务

**前端方式**：访问服务管理页面，点击"添加服务"

**API 方式**：

```bash
curl -X POST http://localhost:8080/api/v1/config/services/langgraph \
  -H "Content-Type: application/json" \
  -d '{
    "service_id": "my-agent",
    "name": "My LangGraph Agent",
    "deployment_url": "http://localhost:2024",
    "graph_id": "agent",
    "session_enabled": true,
    "proxy_mode": "transparent"
  }'
```

### 2. 使用透明代理

注册后，可通过以下路由访问 LangGraph API：

| LangGraph 原始 API | 透明代理路由 |
|---|---|
| `POST /threads` | `POST /api/v1/proxy/my-agent/threads` |
| `POST /threads/{id}/runs/stream` | `POST /api/v1/proxy/my-agent/threads/{id}/runs/stream` |
| `POST /runs/stream` | `POST /api/v1/proxy/my-agent/runs/stream` |
| `GET /assistants` | `GET /api/v1/proxy/my-agent/assistants` |

### 3. 流式对话示例

```python
import httpx

async def chat_stream():
    async with httpx.AsyncClient() as client:
        # 创建 thread
        thread_resp = await client.post(
            "http://localhost:8080/api/v1/proxy/my-agent/threads",
            json={}
        )
        thread_id = thread_resp.json()["thread_id"]
        
        # 流式对话
        async with client.stream(
            "POST",
            f"http://localhost:8080/api/v1/proxy/my-agent/threads/{thread_id}/runs/stream",
            json={
                "input": {"messages": [{"role": "user", "content": "Hello!"}]},
                "stream_mode": "updates"
            }
        ) as response:
            async for chunk in response.aiter_text():
                print(chunk)
```

## 服务配置

### connector_config 配置项

```yaml
connector_config:
  # 代理模式
  proxy_mode: transparent  # transparent | adapter
  
  # 上游服务
  upstream_url: "http://localhost:2024"
  upstream_urls: ["http://server1:2024", "http://server2:2024"]  # 多实例
  
  # LangGraph 配置
  assistant_id: "agent"  # 自动注入到请求体
  
  # 认证
  auth_token: "your-api-key"  # 注入为 X-Api-Key 或 Authorization
  forward_auth: true  # 是否转发原始 Authorization
  
  # 超时（秒）
  timeout_connect: 5
  timeout_read: 300
  timeout_write: 60
  
  # 负载均衡
  load_balance_strategy: round_robin  # round_robin | least_connections | random
```

## LangGraph API 兼容性

透明代理完全兼容 LangGraph Server 的所有 API：

### Assistants API
- `GET /assistants` - 列出 assistants
- `GET /assistants/{assistant_id}` - 获取 assistant
- `POST /assistants` - 创建 assistant
- `POST /assistants/search` - 搜索 assistants

### Threads API
- `POST /threads` - 创建 thread
- `GET /threads/{thread_id}` - 获取 thread
- `PATCH /threads/{thread_id}` - 更新 thread
- `DELETE /threads/{thread_id}` - 删除 thread
- `POST /threads/search` - 搜索 threads

### Runs API
- `POST /threads/{thread_id}/runs` - 创建 run
- `POST /threads/{thread_id}/runs/stream` - 流式 run ⭐
- `POST /threads/{thread_id}/runs/wait` - 等待 run 完成
- `GET /threads/{thread_id}/runs` - 列出 runs
- `GET /threads/{thread_id}/runs/{run_id}` - 获取 run
- `POST /threads/{thread_id}/runs/{run_id}/cancel` - 取消 run

### Stateless Runs
- `POST /runs/stream` - 无状态流式 run ⭐
- `POST /runs/wait` - 无状态等待 run

### State API
- `GET /threads/{thread_id}/state` - 获取状态
- `POST /threads/{thread_id}/state` - 更新状态
- `GET /threads/{thread_id}/history` - 获取历史

## 数据库迁移

执行迁移脚本：

```bash
cd C:\Projects\Agent_Gateway
conda activate ai_gateway

# 查看迁移状态
python database/migrate.py status

# 执行所有待执行的迁移
python database/migrate.py migrate

# 执行指定迁移
python database/migrate.py migrate 003
```

## 健康检查

```bash
# 检查代理服务健康状态
curl http://localhost:8080/api/v1/proxy/my-agent/_health

# 列出所有代理服务
curl http://localhost:8080/api/v1/proxy
```

## 注意事项

1. **assistant_id 自动注入**：如果服务配置了 `assistant_id`，代理会自动将其注入到 `/runs`、`/runs/stream` 等请求体中
2. **错误透传**：上游服务返回的 4xx/5xx 错误会原样返回，不会被网关的通用错误覆盖
3. **流式自动识别**：代理会检测响应的 `Content-Type`，如果是 `text/event-stream` 则自动进入流式模式

