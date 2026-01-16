# Dashboard 虚拟服务显示 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让仪表盘正确显示所有服务类型：AI 助手、LangGraph Agent、代理服务

**Architecture:** 在 services API 中添加虚拟服务（AI 助手），合并数据库中的服务一起返回

**Tech Stack:** FastAPI, React, TypeScript, PostgreSQL

---

## 背景分析

### 当前问题
- `/api/v1/services` 只返回 `services` 表中的服务（代理服务和 LangGraph）
- AI 助手是内置功能，不在 `services` 表中
- 仪表盘"服务健康状态"只显示数据库中的服务

### 目标效果
```
服务健康状态
├── AI 助手 (virtual) - 内置多功能助手
├── local-2024-flash (langgraph) - LangGraph Agent
└── openai-proxy (proxy) - 代理服务
```

---

## Task 1: 修改 Services API 添加虚拟服务

**Files:**
- Modify: `src/api/v1/services.py:145-200`

**Step 1: 查看当前 list_services 实现**

Run: `grep -n "async def list_services" src/api/v1/services.py`

**Step 2: 添加虚拟服务逻辑**

在 `list_services` 函数中，查询数据库服务后，添加 AI 助手作为虚拟服务：

```python
# 在返回前添加虚拟服务
virtual_services = [
    {
        "service_id": "assistant",
        "name": "AI 助手",
        "description": "内置多功能 AI 助手，支持知识库检索、网页搜索、工具调用",
        "service_type": "assistant",
        "endpoint": "/api/v1/assistant",
        "is_virtual": True,
        "enabled": True,
        "created_at": None,
        "updated_at": None,
    }
]

# 合并虚拟服务和数据库服务
all_services = virtual_services + db_services
```

**Step 3: 运行测试验证**

Run: `curl -s http://localhost:8080/api/v1/services | jq '.[] | {service_id, name, service_type}'`
Expected: 应该看到 assistant, langgraph, proxy 三种类型的服务

**Step 4: Commit**

```bash
git add src/api/v1/services.py
git commit -m "feat(api): add AI assistant as virtual service in services list"
```

---

## Task 2: 修改健康检查 API 返回 AI 助手状态

**Files:**
- Modify: `src/api/v1/services.py` (health endpoint)

**Step 1: 查看当前健康检查实现**

Run: `grep -n "health" src/api/v1/services.py`

**Step 2: 添加 AI 助手健康状态**

```python
# 在 health 端点中添加 AI 助手状态
health_status["assistant"] = {
    "status": "healthy" if app.state.assistant_service else "unavailable",
    "last_check": datetime.utcnow().isoformat(),
    "latency_ms": None,  # 内置服务无网络延迟
}
```

**Step 3: 验证健康检查 API**

Run: `curl -s http://localhost:8080/api/v1/services/health | jq`
Expected: 应该包含 assistant 的健康状态

**Step 4: Commit**

```bash
git add src/api/v1/services.py
git commit -m "feat(api): include AI assistant in health check response"
```

---

## Task 3: 更新前端 ServiceCard 支持虚拟服务

**Files:**
- Modify: `web/src/components/ServiceCard.tsx`

**Step 1: 查看当前 ServiceCard 实现**

Run: `head -100 web/src/components/ServiceCard.tsx`

**Step 2: 添加虚拟服务类型支持**

确保 ServiceCard 能正确显示 `service_type: "assistant"` 的服务：
- 使用不同的图标（机器人图标）
- 显示"内置服务"标签
- 正确显示健康状态

**Step 3: 验证前端显示**

在浏览器中打开仪表盘，确认 AI 助手卡片正确显示

**Step 4: Commit**

```bash
git add web/src/components/ServiceCard.tsx
git commit -m "feat(ui): support virtual service type in ServiceCard"
```

---

## Task 4: 添加服务类型过滤器（可选）

**Files:**
- Modify: `web/src/pages/Dashboard.tsx`

**Step 1: 添加服务类型筛选**

```typescript
const [serviceTypeFilter, setServiceTypeFilter] = useState<string>("all");

const filteredServices = services.filter(s =>
  serviceTypeFilter === "all" || s.service_type === serviceTypeFilter
);
```

**Step 2: 添加筛选 UI**

```tsx
<Select
  value={serviceTypeFilter}
  onChange={setServiceTypeFilter}
  options={[
    { value: "all", label: "全部服务" },
    { value: "assistant", label: "AI 助手" },
    { value: "langgraph", label: "LangGraph" },
    { value: "proxy", label: "代理服务" },
  ]}
/>
```

**Step 3: Commit**

```bash
git add web/src/pages/Dashboard.tsx
git commit -m "feat(ui): add service type filter to dashboard"
```

---

## Task 5: 端到端测试

**Step 1: 验证 API**

```bash
# 验证服务列表包含 AI 助手
curl -s http://localhost:8080/api/v1/services | jq '.[] | {service_id, service_type}'

# 验证健康检查包含 AI 助手
curl -s http://localhost:8080/api/v1/services/health | jq
```

**Step 2: 验证用量统计**

```bash
# 发送 AI 助手请求
curl -X POST http://localhost:8080/api/v1/assistant/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"message": "test", "model_id": "qwen-turbo"}'

# 验证用量按服务分类
curl -s "http://localhost:8080/api/v1/usage/breakdown?dimension=service" \
  -H "Authorization: Bearer <token>" | jq
```

**Step 3: 验证前端**

1. 打开 http://localhost:5173
2. 确认"服务健康状态"显示 AI 助手
3. 确认 AI 助手卡片显示正确的健康状态
4. 确认用量统计正确按服务分类

---

## 验证清单

- [ ] `/api/v1/services` 返回 AI 助手作为虚拟服务
- [ ] `/api/v1/services/health` 返回 AI 助手健康状态
- [ ] 仪表盘"服务健康状态"显示 AI 助手卡片
- [ ] AI 助手卡片显示正确的图标和状态
- [ ] 用量统计按 `service_id` 正确分类（assistant/langgraph/proxy）
- [ ] 外部 API 调用 AI 助手正常工作
- [ ] 限流、鉴权、计费等 AOP 能力正常

---

## 参考资料

- [AI Gateway Best Practices 2025](https://www.truefoundry.com/blog/rate-limiting-in-llm-gateway)
- [LLM Gateway Design Patterns](https://collabnix.com/llm-gateway-patterns-rate-limiting-and-load-balancing-guide/)
- [Azure AI Gateway Architecture](https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities)
