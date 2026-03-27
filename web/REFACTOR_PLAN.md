# Playground 前端重构计划

> 基于官方 agent-chat-ui + @langchain/langgraph-sdk useStream hook

## 根因

当前 `usePlaygroundStream.ts` (1900行) 手动管理了所有 LangGraph SDK 该管的事情：
- 手动 `POST /threads` 创建 thread → **阻塞 handleSend → TTFT 22s**
- 手动 SSE parsing + tool_result 提取
- 手动 stall/max/idle guard (复杂且脆弱)
- 手动消息状态管理 (streamState, seenMessageIds, etc.)

## 目标架构

```
useStream (SDK)
  ├── 自动 thread 创建 (threadId: null → onThreadId 回调)
  ├── 自动 SSE 解析 + 消息拼接
  ├── 自动 tool call 追踪 (toolProgress)
  ├── 自动历史加载 (fetchStateHistory: true)
  └── 自动重连 (reconnectOnMount)

usePlaygroundSessions (保留, 精简)
  ├── Gateway session CRUD (侧边栏)
  ├── threadId ↔ sessionId 映射
  └── 不再管 thread 创建

UI Components (保留)
  ├── ChatMessageItem (适配 SDK Message 类型)
  ├── ToolCallBlock (适配 toolProgress)
  └── StatsBadge (从 onFinish 回调获取)
```

## SDK 安装

```bash
# npm 有 arborist bug, 已手动拷贝到 node_modules
# 正式修复: 在 package.json 添加依赖后用 pnpm 安装
# web/node_modules/@langchain/langgraph-sdk v1.8.0 已就位
```

需要在 package.json 添加:
```json
"@langchain/langgraph-sdk": "^1.8.0"
```

## 重构步骤

### Step 1: 创建新的 streaming hook (useImamStream.ts)

```typescript
// web/src/pages/playground/hooks/useImamStream.ts
import { useStream } from "@langchain/langgraph-sdk/react";
import type { Message } from "@langchain/langgraph-sdk";

interface ImamStreamState {
  messages: Message[];
}

export function useImamStream({
  serviceId,
  sessionId,
  threadId,
  onThreadId,
  authToken,
}: {
  serviceId: string;
  sessionId: string | null;
  threadId: string | null;
  onThreadId: (id: string) => void;
  authToken: string | null;
}) {
  const stream = useStream<ImamStreamState>({
    // 通过我们的 Gateway proxy 连接 LangGraph
    apiUrl: `/api/v1/proxy/${serviceId}`,
    assistantId: "imam",  // LangGraph assistant ID

    // Thread 生命周期: null → 自动创建 → onThreadId 回调
    threadId,
    onThreadId: (id) => {
      onThreadId(id);
      // 同步更新 Gateway session metadata
      if (sessionId) {
        updateSession(sessionId, {
          metadata: { langgraph_thread_id: id }
        }).catch(console.error);
      }
    },

    // 自动加载历史
    fetchStateHistory: true,

    // Auth header 注入
    defaultHeaders: authToken
      ? { Authorization: `Bearer ${authToken}` }
      : undefined,

    // 回调
    onError: (err) => console.error("[ImamStream] Error:", err),
    onFinish: (state, run) => {
      // 保存消息到 Gateway session
      // 提取 stats (duration, tokens)
    },
  });

  return stream;
}
```

### Step 2: 精简 usePlaygroundSessions.ts

移除的代码:
- `pendingThreadRef` 及所有 thread 创建逻辑 (line 174-208)
- `sessionThreadIdRef` 的手动维护
- thread 相关的 loading 状态

保留的代码:
- Gateway session CRUD (listSessions, createSession, deleteSession)
- 侧边栏 session 列表管理
- Active session tracking

新增:
- `threadId` state (从 session metadata 读取)
- `setThreadId` 回调 (写入 session metadata)

### Step 3: 适配 UI 组件

`ChatMessageItem.tsx`:
- 当前接收 `ChatMessage` 类型
- 需要适配 SDK 的 `Message` 类型 (包含 tool_calls, content blocks)
- tool_calls 从 `Message.tool_calls` 直接获取,不需要手动提取

`ToolCallBlock.tsx`:
- 当前从 streamState 手动构建 tool call 数据
- 改为从 `stream.toolProgress` 获取实时进度
- 或从 `Message.tool_calls` + 对应 `tool` message 获取结果

### Step 4: 删除旧代码

删除 `usePlaygroundStream.ts` 中的:
- 手动 SSE fetch + EventSource 逻辑 (~500 行)
- streamState/streamReducer 状态管理 (~200 行)
- stall/max/idle guard timers (~100 行)
- tool_result 提取 + seenMessageIds 去重 (~200 行)
- fallback recovery (runs/wait) (~100 行)
- 手动 thread 创建 + await (~100 行)

估计可以从 1900 行减到 ~300 行。

### Step 5: 测试验证

1. 新建对话 → 确认 thread 自动创建 (TTFT < 5s)
2. 发送 Islamic 问题 → 确认 streaming + tool calls 显示
3. 多轮对话 → 确认上下文保持
4. 切换历史对话 → 确认历史加载 (< 1s)
5. 跑题拒绝 → 确认正确处理
6. 取消/停止 → 确认 stop() 工作

## 关键 API 映射

| 当前实现 | SDK 替代 |
|---------|---------|
| `POST /proxy/{id}/threads` (手动) | `threadId: null` (自动) |
| `POST /proxy/{id}/threads/{tid}/runs/stream` (手动 fetch) | `stream.submit(input)` |
| 手动 SSE 解析 | SDK 内置 |
| `streamState.toolCalls` | `stream.toolProgress` |
| `streamState.content` | `stream.messages` |
| `cancelStreamTurn()` | `stream.stop()` |
| `getSessionHistory()` | `fetchStateHistory: true` |
| stall/max/idle guards | SDK auto-reconnect |

## 风险和注意事项

1. **Proxy 兼容性**: `useStream` 期望标准 LangGraph API。我们的 proxy 添加了 auth headers, billing 拦截等。需要确认 SDK 的请求格式和我们的 proxy 兼容。

2. **消息格式**: SDK 使用 `Message` 类型 (BaseMessage from LangChain)。我们的 `ChatMessage` 类型不同。需要写 adapter。

3. **Stats**: SDK 没有直接暴露 token count / duration。需要从 `onFinish` 回调或 metadata 提取。

4. **Session 持久化**: SDK 管 thread,我们还需要管 Gateway session (标题、元数据)。两者需要同步。

5. **多 Service 支持**: 当前 playground 支持切换不同 service (不只 Imam)。useStream 的 `apiUrl` 和 `assistantId` 需要动态切换。
