# KV-Cache 上下文管理优化设计

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现 Manus 风格的三层缓存架构，最大化 KV-cache 命中率，降低 90% 的 token 成本

**Architecture:** 三层缓存块（静态前缀 → 会话上下文 → 动态输入），通过 cache_control 显式标记缓存断点

**Tech Stack:** Python, Gemini API, DashScope API, React TypeScript

---

## 一、核心原理

### KV-Cache 工作机制

KV-Cache 的核心原理：**如果请求的前 N 个 token 与之前的请求完全相同，这些 token 的 KV 值可以直接复用**。

```
请求 1: [System Prompt][KB Context A][History 1-5][User Q1]
请求 2: [System Prompt][KB Context A][History 1-5][User Q2]
         ↑______________ 缓存命中 ______________↑
```

### 成本影响

| Provider | 缓存 Token 价格 | 未缓存价格 | 折扣 |
|----------|----------------|-----------|------|
| Gemini 2.5 | $0.075/M | $0.75/M | **90%** |
| DashScope | ~$0.0005/M | ~$0.002/M | **75%** |

---

## 二、三层缓存架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 1: Static Prefix                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ System Prompt (deterministic, no timestamps)              │ │
│  │ + Tool Definitions (sorted by name, stable JSON)          │ │
│  │ + User Preferences / Role Config                          │ │
│  │                                                           │ │
│  │ cache_control: {"type": "ephemeral"}  ← 缓存断点 1        │ │
│  └───────────────────────────────────────────────────────────┘ │
│  预期缓存命中率: ~95% (跨所有用户、所有会话)                    │
├─────────────────────────────────────────────────────────────────┤
│                    Layer 2: Session Context                     │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ KB Context (sorted by dataset_id, then score)             │ │
│  │ + Web Search Results (if any)                             │ │
│  │ + Conversation History                                    │ │
│  │   - Message 1 (user)                                      │ │
│  │   - Message 2 (assistant)                                 │ │
│  │   - ...                                                   │ │
│  │   - Tool Call N + Result N                                │ │
│  │                                                           │ │
│  │ cache_control: {"type": "ephemeral"}  ← 缓存断点 2        │ │
│  └───────────────────────────────────────────────────────────┘ │
│  预期缓存命中率: ~80% (同会话多轮复用)                          │
├─────────────────────────────────────────────────────────────────┤
│                    Layer 3: Current Input                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ Current User Message                                      │ │
│  │ (no cache - always dynamic)                               │ │
│  └───────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、实现任务

### Task 1: 创建缓存优化器模块

**Files:**
- Create: `src/services/assistant/cache_optimizer.py`

**Step 1: 创建数据结构**

```python
"""
KV-Cache Optimization for Assistant Service

Implements Manus-style context caching with three cache layers:
- Layer 1: Static prefix (system prompt + tools) - cross-session reuse
- Layer 2: Session context (KB + history) - intra-session reuse
- Layer 3: Current input (dynamic)
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum
import hashlib
import json


class CacheBreakpoint(Enum):
    """Cache control markers for provider APIs."""
    EPHEMERAL = "ephemeral"  # Short-lived cache (5-60 min)


@dataclass
class CacheConfig:
    """Configuration for KV-cache optimization."""
    enable_layer1_cache: bool = True  # System prefix caching
    enable_layer2_cache: bool = True  # Session context caching
    layer1_ttl_minutes: int = 60      # System prefix TTL
    layer2_ttl_minutes: int = 10      # Session context TTL
    min_cacheable_tokens: int = 1024  # Minimum tokens for caching


@dataclass
class CacheMetrics:
    """Metrics for cache performance tracking."""
    layer1_hit: bool = False
    layer2_hit: bool = False
    cached_tokens: int = 0
    total_input_tokens: int = 0
    cache_hit_rate: float = 0.0
    estimated_savings_usd: float = 0.0
    system_prefix_hash: str = ""
```

**Step 2: 实现核心优化器类**

```python
class ContextCacheOptimizer:
    """
    Optimizes context for maximum KV-cache hit rate.

    Key strategies:
    1. Deterministic system prompt (no timestamps, sorted tools)
    2. Stable context ordering (system → KB → history → user)
    3. Explicit cache breakpoints for provider APIs
    """

    def __init__(self, config: CacheConfig = None):
        self.config = config or CacheConfig()
        self._system_prefix_hash: Optional[str] = None

    def build_optimized_messages(
        self,
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]],
        kb_context: Optional[str],
        web_context: Optional[str],
        history: List[Dict[str, Any]],
        current_message: str,
        provider: str,  # "gemini" | "dashscope"
    ) -> List[Dict[str, Any]]:
        """Build messages with optimal cache structure."""
        messages = []

        # === Layer 1: Static Prefix ===
        system_content = self._build_deterministic_system(system_prompt, tools)
        system_msg = {"role": "system", "content": system_content}

        if self.config.enable_layer1_cache:
            system_msg["cache_control"] = {"type": "ephemeral"}

        messages.append(system_msg)

        # === Layer 2: Session Context ===
        if kb_context:
            messages.append({"role": "user", "content": f"[参考资料]\n{kb_context}"})
            messages.append({"role": "assistant", "content": "我已阅读参考资料，将基于这些信息回答问题。"})

        if web_context:
            messages.append({"role": "user", "content": f"[网络搜索结果]\n{web_context}"})
            messages.append({"role": "assistant", "content": "我已获取最新的网络搜索结果。"})

        for msg in history:
            messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            })

        if self.config.enable_layer2_cache and len(messages) > 1:
            messages[-1]["cache_control"] = {"type": "ephemeral"}

        # === Layer 3: Current Input ===
        messages.append({"role": "user", "content": current_message})

        return messages

    def _build_deterministic_system(
        self,
        base_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Build byte-identical system prompt for cache stability."""
        parts = [base_prompt.strip()]

        if tools:
            sorted_tools = sorted(tools, key=lambda t: t.get("function", {}).get("name", ""))
            tools_section = "\n\n## 可用工具\n"
            for tool in sorted_tools:
                func = tool.get("function", {})
                name = func.get("name", "unknown")
                desc = func.get("description", "")
                tools_section += f"\n### {name}\n{desc}\n"
            parts.append(tools_section)

        system_content = "\n".join(parts)
        self._system_prefix_hash = hashlib.md5(system_content.encode()).hexdigest()[:8]

        return system_content

    def calculate_cache_savings(
        self,
        total_tokens: int,
        cached_tokens: int,
        provider: str
    ) -> float:
        """Calculate estimated cost savings from cache hits."""
        pricing = {
            "gemini": {"cached": 0.075, "uncached": 0.75},
            "dashscope": {"cached": 0.0005, "uncached": 0.002},
        }

        rates = pricing.get(provider, pricing["dashscope"])
        full_cost = (total_tokens / 1_000_000) * rates["uncached"]
        actual_cost = (cached_tokens / 1_000_000) * rates["cached"] + \
                      ((total_tokens - cached_tokens) / 1_000_000) * rates["uncached"]

        return full_cost - actual_cost

    def parse_cache_metrics(
        self,
        response_usage: Dict[str, Any],
        provider: str
    ) -> CacheMetrics:
        """Extract cache metrics from LLM API response."""
        metrics = CacheMetrics()

        if provider == "gemini":
            metrics.total_input_tokens = response_usage.get("promptTokenCount", 0)
            metrics.cached_tokens = response_usage.get("cachedContentTokenCount", 0)
        elif provider == "dashscope":
            usage = response_usage.get("prompt_tokens_details", {})
            metrics.total_input_tokens = response_usage.get("input_tokens", 0)
            metrics.cached_tokens = usage.get("cached_tokens", 0)

        if metrics.total_input_tokens > 0:
            metrics.cache_hit_rate = metrics.cached_tokens / metrics.total_input_tokens

        metrics.estimated_savings_usd = self.calculate_cache_savings(
            metrics.total_input_tokens,
            metrics.cached_tokens,
            provider
        )
        metrics.system_prefix_hash = self._system_prefix_hash or ""

        return metrics
```

**Step 3: 验证模块可导入**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
~/miniconda3/bin/conda run -n ai_gateway python -c "from src.services.assistant.cache_optimizer import ContextCacheOptimizer; print('OK')"
```

---

### Task 2: 添加缓存指标 SSE 事件

**Files:**
- Modify: `src/api/schemas/assistant.py`

**Step 1: 添加 CacheMetricsEvent schema**

在 SSEEventType 枚举后添加：

```python
class CacheMetricsEvent(BaseModel):
    """Cache performance metrics for monitoring."""
    layer1_hit: bool = Field(default=False, description="Whether Layer 1 (system prefix) cache was hit")
    layer2_hit: bool = Field(default=False, description="Whether Layer 2 (session context) cache was hit")
    total_input_tokens: int = Field(default=0, description="Total input tokens")
    cached_tokens: int = Field(default=0, description="Number of tokens served from cache")
    cache_hit_rate: float = Field(default=0.0, description="Cache hit rate (0-1)")
    estimated_savings_usd: float = Field(default=0.0, description="Estimated cost savings in USD")
    system_prefix_hash: str = Field(default="", description="Hash of Layer 1 cache key for debugging")
```

**Step 2: 在 SSEEventType 中添加事件类型**

```python
class SSEEventType(str, Enum):
    # ... existing events ...
    CACHE_METRICS = "cache_metrics"  # Cache performance metrics
```

---

### Task 3: 集成到 AssistantService

**Files:**
- Modify: `src/services/assistant/assistant_service.py`

**Step 1: 导入缓存优化器**

```python
from .cache_optimizer import ContextCacheOptimizer, CacheConfig, CacheMetrics
```

**Step 2: 在 __init__ 中初始化**

```python
def __init__(self, ...):
    # ... existing code ...
    self.cache_optimizer = ContextCacheOptimizer(CacheConfig())
```

**Step 3: 修改 _build_messages 方法**

替换现有的 _build_messages 方法，使用 cache_optimizer.build_optimized_messages

**Step 4: 在响应中解析并发送缓存指标**

在 chat_stream 方法的 usage 事件处理后添加：

```python
# Parse and emit cache metrics
if usage_data:
    cache_metrics = self.cache_optimizer.parse_cache_metrics(
        usage_data,
        provider=self._get_provider_from_model(model_id)
    )
    yield self._create_sse_event(
        SSEEventType.CACHE_METRICS,
        cache_metrics.__dict__
    )
```

---

### Task 4: 前端显示缓存指标

**Files:**
- Modify: `web/src/pages/assistant/types.ts`
- Modify: `web/src/pages/assistant/components/ChatMessage.tsx`

**Step 1: 添加 CacheMetrics 类型**

```typescript
export interface CacheMetrics {
  layer1_hit: boolean;
  layer2_hit: boolean;
  total_input_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
  estimated_savings_usd: number;
  system_prefix_hash: string;
}

// 在 ChatMessage 接口中添加
cacheMetrics?: CacheMetrics;
```

**Step 2: 在 StatsBadge 中显示缓存指标**

```tsx
{message.cacheMetrics && message.cacheMetrics.cache_hit_rate > 0 && (
  <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900/40 text-emerald-600 dark:text-emerald-400">
    <Zap className="h-3 w-3" />
    {(message.cacheMetrics.cache_hit_rate * 100).toFixed(0)}% cached
    {message.cacheMetrics.estimated_savings_usd > 0 && (
      <span className="text-emerald-500 ml-1">
        (-${message.cacheMetrics.estimated_savings_usd.toFixed(4)})
      </span>
    )}
  </span>
)}
```

---

### Task 5: 处理前端 SSE 事件

**Files:**
- Modify: `web/src/pages/assistant/index.tsx`

**Step 1: 在 SSEEventType 中添加 CACHE_METRICS**

```typescript
export enum SSEEventType {
  // ... existing ...
  CACHE_METRICS = "cache_metrics",
}
```

**Step 2: 在 sendMessage 的事件处理中添加**

```typescript
case SSEEventType.CACHE_METRICS:
  setMessages((prev) =>
    prev.map((m) =>
      m.id === assistantMessageId
        ? { ...m, cacheMetrics: parsed.data }
        : m
    )
  );
  break;
```

---

## 四、测试验证

### 测试场景

| 测试场景 | 预期结果 | 验证方法 |
|---------|---------|---------|
| 同会话多轮对话 | Layer 2 缓存命中率 > 80% | 检查 `cached_tokens` 字段 |
| 跨会话相同工具 | Layer 1 缓存命中率 > 95% | 比较 `system_prefix_hash` |
| 修改系统提示 | Layer 1 缓存失效 | Hash 变化，命中率下降 |
| Agent 多次工具调用 | 每次迭代累积缓存 | 第 N 次迭代命中率更高 |

### 验证命令

```bash
# 测试缓存效果
curl -X POST http://localhost:8080/api/v1/assistant/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-session",
    "message": "Hello",
    "model_id": "gemini-2.5-flash"
  }' | grep -E "cached_tokens|cache_hit_rate"
```

---

## 五、预期收益

- **成本降低**: Gemini 90% 折扣，DashScope ~75% 折扣
- **延迟降低**: 缓存命中时 TTFT 显著减少
- **多轮 Agent**: 每次迭代累积缓存，越到后期效果越好
