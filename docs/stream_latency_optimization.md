# 流式响应延迟优化分析报告

## 问题描述

对话测试模块的首 token 延迟比直接在 Langsmith 调试时多出 3-5 秒（甚至更多），表明网关层存在异常延迟。

## 关键发现

### 1. 根本原因：BaseHTTPMiddleware 缓冲 StreamingResponse

**核心问题**：Starlette 的 `BaseHTTPMiddleware` 会缓冲整个 `StreamingResponse` 响应体，导致流式响应无法真正"流式"发送。

以下中间件都使用了 `BaseHTTPMiddleware`，会导致流式响应被缓冲：
- `AuthMiddleware` (`src/core/middleware/auth.py`)
- `RateLimitMiddleware` (`src/core/middleware/rate_limit_http.py`)
- `RequestLoggingMiddleware` (`src/core/middleware/request_logging.py`)
- `TracingMiddleware` (`src/core/observability/tracing.py`)

**重要**：即使这些中间件内部有路径排除逻辑（whitelist/exclude_paths），只要继承自 `BaseHTTPMiddleware`，`call_next()` 的调用本身就会触发响应缓冲。

### 2. 调试数据分析

从 debug_timing 数据可以看出延迟分布：

```json
{
  "preflight_ms": 21031.69,        // ❌ 应该 < 10ms，实际 21 秒
  "first_chunk_ms": 24841.67,      // 从 t0 到首个 chunk
  "upstream_first_data_ms": 1015.72,  // ✅ LangGraph 响应正常 ~1s
  "langgraph_thread_ms": 6.09,     // ✅ 线程操作正常
  "langgraph_header_ms": 9.39,     // ✅ LangGraph 响应头正常
  "gateway_timing": {
    "service_lookup_ms": 0.01,     // ✅ 正常
    "validate_ms": 0.02,           // ✅ 正常
    "rate_limit_ms": 0.01,         // ✅ 正常
    "adapter_first_chunk_ms": 3809.55  // ✅ 适配器层正常（包含 LangGraph 处理）
  }
}
```

**关键发现**：
- LangGraph 本身只需要 ~1 秒返回首数据
- 网关的 dispatcher 和 adapter 层工作正常
- **问题在于 `preflight_ms` 显示 21 秒**，这说明 `event_generator` 的首次迭代被大幅延迟

### 3. 问题时序分析

```
t0: stream() 函数开始
│
├─ preflight_ms 计算 (~几ms)
│
├─ return StreamingResponse(event_generator())  <- 立即返回
│
│  ... (客户端连接，中间件处理) ...
│
├─ event_generator 首次迭代 <- ❌ 在这里被 BaseHTTPMiddleware 阻塞约 20 秒
│  │
│  ├─ yield heartbeat
│  ├─ yield thinking_chunk
│  └─ async for chunk in dispatcher.stream()
│       └─ LangGraph 调用 (~1 秒)
│
└─ 客户端收到首个 chunk
```

## 已完成的优化

### 1. 配置优化
- **文件**: `config/env.local`
- **改动**: 将所有 `localhost` 改为 `127.0.0.1`，避免 Windows 的 IPv6/IPv4 DNS 解析延迟

### 2. 中间件白名单
- **文件**: `src/main.py`
- **改动**: 将 `/api/v1/stream` 添加到以下中间件的排除列表：
  - `AuthMiddleware.whitelist_paths`
  - `RateLimitMiddleware.whitelist_paths`
  - `RequestLoggingMiddleware.exclude_paths`
  - `TracingMiddleware.exclude_paths`

### 3. AnonymousIdentityMiddleware 纯 ASGI 化
- **文件**: `src/core/auth/anonymous_middleware.py`
- **改动**: 将其从 `BaseHTTPMiddleware` 转换为纯 ASGI 中间件，并添加流式路径检测

### 4. 调试时间追踪
- **文件**: `src/api/v1/stream.py`
- **改动**: 添加了详细的 timing 日志，支持 `X-Debug-Stream-Timing: 1` 头部

### 5. 流式中间件基础设施
- **文件**: `src/core/middleware/streaming.py`
- **改动**: 创建了 `StreamingBypassMiddleware` 框架（待完善）

## 待完成的核心任务

### 任务 1：将所有 BaseHTTPMiddleware 转换为纯 ASGI（最重要）

需要将以下中间件从 `BaseHTTPMiddleware` 转换为纯 ASGI 实现：

1. **AuthMiddleware** (`src/core/middleware/auth.py`)
   - 对于流式路径，直接传递请求，不做任何响应处理
   - 对于非流式路径，保持现有逻辑

2. **RateLimitMiddleware** (`src/core/middleware/rate_limit_http.py`)
   - 同上

3. **RequestLoggingMiddleware** (`src/core/middleware/request_logging.py`)
   - 同上

4. **TracingMiddleware** (`src/core/observability/tracing.py`)
   - 同上

**参考实现**: `src/core/auth/anonymous_middleware.py` 已转换为纯 ASGI

### 任务 2：优化 stream.py 的响应时机

当前代码在 `event_generator` 中先发送 heartbeat 和 thinking 事件，但由于中间件缓冲，这些事件无法立即发送。

转换中间件后，需要验证：
1. heartbeat 事件是否能立即触发 HTTP 响应头
2. thinking 事件是否能在 dispatcher.stream() 调用前就发送到客户端

### 任务 3：前端优化（可选增强）

`web/src/pages/Playground.tsx` 和 `web/src/lib/sse.ts` 的优化已部分完成，可以进一步：
1. 添加首 token 延迟的可视化显示
2. 使用 `requestAnimationFrame` 批量更新 UI

## 验证方法

1. 使用 `X-Debug-Stream-Timing: 1` 头部发送请求
2. 检查 `preflight_ms` 是否 < 10ms
3. 检查 `first_chunk_ms` 是否 ≈ `adapter_first_chunk_ms`

理想情况下的 timing：
```json
{
  "preflight_ms": 5,              // < 10ms
  "first_chunk_ms": 1200,         // ≈ LangGraph 处理时间
  "upstream_first_data_ms": 1000, // LangGraph 首响应
  "adapter_first_chunk_ms": 1100  // 略大于 upstream
}
```

## 相关文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `src/main.py` | 已修改 | 中间件配置，添加了白名单 |
| `src/api/v1/stream.py` | 已修改 | 添加了 debug timing |
| `src/core/auth/anonymous_middleware.py` | 已转换 | 纯 ASGI 实现 |
| `src/core/middleware/auth.py` | **待转换** | 需要纯 ASGI 化 |
| `src/core/middleware/rate_limit_http.py` | **待转换** | 需要纯 ASGI 化 |
| `src/core/middleware/request_logging.py` | **待转换** | 需要纯 ASGI 化 |
| `src/core/observability/tracing.py` | **待转换** | 需要纯 ASGI 化 |
| `src/core/middleware/streaming.py` | 已创建 | 流式中间件框架 |
| `config/env.local` | 已修改 | localhost → 127.0.0.1 |

## 参考资料

- [Starlette BaseHTTPMiddleware 缓冲问题](https://github.com/encode/starlette/issues/919)
- [StreamingResponse 最佳实践](https://www.starlette.io/responses/#streamingresponse)

