# AI Assistant SDK 设计计划书

> **版本**: v1.0 | **日期**: 2026-04-03 | **作者**: AI Architecture Team
> **状态**: Draft — 待评审

---

## 1. 执行摘要

本文档设计一套多语言 AI Assistant SDK，将 Assistant Service（port 8093）的全部能力封装为开发者友好的客户端库。目标：让 Wahda、HalalMoney 等外部 App 用 **5 行代码完成初始化、10 行代码发出第一条流式消息**，直接变成 AI 原生应用。

SDK 提供三层接入模式（轻量 / 标准 / 完整），输出 Python SDK、TypeScript SDK 和 OpenAPI 3.1 Spec（供 Swift/Kotlin 代码生成）。

---

## 2. 架构总览

```
┌──────────────────────────────────────────────────────┐
│                    External Apps                      │
│  ┌──────────┐  ┌───────────┐  ┌───────────────────┐  │
│  │  Wahda   │  │ HalalMoney│  │  Other Tenants    │  │
│  │ (Swift)  │  │ (Kotlin)  │  │  (Any Language)   │  │
│  └────┬─────┘  └─────┬─────┘  └────────┬──────────┘  │
│       │              │                  │             │
│  ┌────▼──────────────▼──────────────────▼──────────┐  │
│  │           AI Assistant SDK Layer                 │  │
│  │  ┌──────┐ ┌──────┐ ┌───────┐ ┌──────┐ ┌─────┐  │  │
│  │  │ Auth │ │ Chat │ │Session│ │  KB  │ │Image│  │  │
│  │  └──────┘ └──────┘ └───────┘ └──────┘ └─────┘  │  │
│  │  ┌──────┐ ┌──────┐ ┌───────┐ ┌──────────────┐  │  │
│  │  │Skills│ │ MCP  │ │Artfct │ │ SSE Stream   │  │  │
│  │  └──────┘ └──────┘ └───────┘ │ Engine       │  │  │
│  │                               └──────────────┘  │  │
│  └─────────────────────┬───────────────────────────┘  │
│                        │ HTTPS + SSE                  │
└────────────────────────┼──────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────┐
│              Assistant Service (8093)                   │
│  ┌────────────────────────────────────────────────┐    │
│  │              REST API Layer                     │    │
│  │  /assistant/chat/stream  /assistant/sessions    │    │
│  │  /assistant/models       /assistant/artifacts   │    │
│  │  /assistant/generate-image  /assistant/tools    │    │
│  └──────────────────┬─────────────────────────────┘    │
│  ┌──────────────────▼─────────────────────────────┐    │
│  │           AssistantService Core                  │    │
│  │  ModelRegistry │ AgentLoop │ ToolOrchestrator   │    │
│  │  ContextMgr │ SessionMgr │ MemoryService       │    │
│  │  RAG/KB │ Skills │ MCP │ ImageGen │ Artifacts   │    │
│  └─────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Infrastructure: Redis │ S3/OSS │ PostgreSQL    │    │
│  └─────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────┘
```

---

## 3. 现有 API 端点梳理

基于 `src/api/v1/assistant.py` 的完整分析，按功能域分组：

### 3.1 Chat（核心对话）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| POST | `/assistant/chat` | 非流式聊天 | `chat` |
| POST | `/assistant/chat/stream` | SSE 流式聊天 | `chat` |
| POST | `/assistant/tasks/{task_id}/cancel` | 取消运行中的任务 | `chat` |
| GET | `/assistant/runs/{run_id}` | 查询 run 状态 | `chat` |

### 3.2 Session（会话管理）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| POST | `/assistant/sessions` | 创建会话 | `sessions` |
| GET | `/assistant/sessions` | 会话列表（分页） | `sessions` |
| GET | `/assistant/sessions/{id}` | 会话详情 | `sessions` |
| DELETE | `/assistant/sessions/{id}` | 删除会话 | `sessions` |
| GET | `/assistant/sessions/{id}/history` | 消息历史（分页） | `sessions` |

### 3.3 Knowledge Base（知识库）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| GET | `/assistant/datasets` | 可用数据集列表 | `knowledge` |

> 注：知识库检索通过 `chat` 请求的 `kb_dataset_ids` / `kb_mode` 参数触发，不是独立端点。

### 3.4 Discovery（能力发现）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| GET | `/assistant/models` | 可用模型列表 | `models` |
| GET | `/assistant/config` | 功能配置 | `config` |
| GET | `/assistant/tools` | 可用工具列表 | `tools` |
| GET | `/assistant/policies` | 网关策略 | `config` |

### 3.5 Image Generation（AI 生图）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| POST | `/assistant/generate-image` | 同步生图 | `images` |
| POST | `/assistant/generate-image-async` | 异步生图（返回 task_id） | `images` |
| GET | `/assistant/image-task/{task_id}` | 轮询异步任务状态 | `images` |

### 3.6 Artifacts（文件存储）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| POST | `/assistant/artifacts` | 创建 artifact | `artifacts` |
| GET | `/assistant/artifacts/{id}` | artifact 元数据 | `artifacts` |
| GET | `/assistant/artifacts/{id}/download` | 下载文件 | `artifacts` |
| GET | `/assistant/sessions/{id}/artifacts` | 会话内 artifacts | `artifacts` |
| DELETE | `/assistant/artifacts/{id}` | 删除 artifact | `artifacts` |

### 3.7 Tool Approval（工具审批）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| POST | `/assistant/approvals/{id}` | 批准/拒绝工具调用 | `tools` |

### 3.8 Metrics（可观测性）

| 方法 | 路径 | 说明 | SDK 模块 |
|------|------|------|---------|
| GET | `/assistant/sessions/{id}/metrics` | 会话级指标 | `metrics` |
| GET | `/assistant/metrics/tenant` | 租户级聚合指标 | `metrics` |

---

## 4. SDK 模块设计

### 4.1 模块架构

```
ai_assistant_sdk/
├── client.py              # AssistantClient 主入口
├── auth.py                # 认证管理
├── chat.py                # 对话模块（含流式）
├── sessions.py            # 会话管理
├── knowledge.py           # 知识库
├── images.py              # AI 生图
├── artifacts.py           # 文件存储
├── tools.py               # 工具 & MCP
├── skills.py              # 技能系统
├── metrics.py             # 可观测性
├── streaming/
│   ├── sse_parser.py      # SSE 协议解析
│   ├── event_handler.py   # 事件分发
│   └── reconnect.py       # 断线重连
├── models/
│   ├── request.py         # 请求类型
│   ├── response.py        # 响应类型
│   ├── events.py          # 66 种 SSE 事件类型
│   └── enums.py           # 枚举定义
├── transport/
│   ├── http.py            # HTTP 客户端
│   ├── sse.py             # SSE 传输层
│   └── retry.py           # 重试策略
└── exceptions.py          # 异常体系
```

### 4.2 Auth 模块

SDK 支持两种认证模式，对开发者完全透明：

```python
# 认证通过构造函数一次性配置，后续所有调用自动携带
class AuthConfig:
    api_key: str              # X-API-Key header
    tenant_id: str            # X-Tenant-Id header
    base_url: str             # Assistant Service URL
    jwt_token: str | None     # 可选：JWT（用于登录用户代理）
    user_id: str | None       # 可选：覆盖 user_id
    timeout: float = 30.0     # 默认超时
    max_retries: int = 3      # 重试次数
```

**HTTP 拦截器自动注入的 Headers：**

```
X-API-Key: {api_key}
X-Tenant-Id: {tenant_id}
Authorization: Bearer {jwt_token}   # 如果提供
Content-Type: application/json
X-SDK-Version: ai-assistant-sdk/1.0.0
X-Request-Id: {uuid4}              # 每个请求唯一
```

### 4.3 Chat 模块（核心）

```python
class ChatModule:
    # 非流式
    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model_id: str = "gemini-2.5-flash",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        kb_dataset_ids: list[str] | None = None,
        kb_mode: Literal["auto", "tool", "off"] = "auto",
        web_search: bool = False,
        file_paths: list[str] | None = None,
        enable_planning: bool = False,
        execution_profile: Literal["safe", "balanced", "power"] = "balanced",
        skills_enabled: bool | None = None,
    ) -> ChatResponse

    # 流式 — 返回异步迭代器
    async def stream(
        self,
        message: str,
        **kwargs,  # 同 send() 参数
    ) -> AsyncIterator[StreamEvent]

    # 带回调的流式（更适合 UI 绑定）
    async def stream_with_handlers(
        self,
        message: str,
        *,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        on_context: Callable[[list[Context]], None] | None = None,
        on_error: Callable[[ErrorEvent], None] | None = None,
        on_done: Callable[[DoneEvent], None] | None = None,
        **kwargs,
    ) -> ChatResponse

    # 取消
    async def cancel(self, task_id: str) -> None

    # 查询运行状态
    async def get_run(self, run_id: str) -> RunStatus
```

### 4.4 Session 模块

```python
class SessionModule:
    async def create(self, metadata: dict | None = None) -> Session
    async def list(self, limit: int = 50, offset: int = 0) -> PaginatedList[Session]
    async def get(self, session_id: str) -> Session
    async def delete(self, session_id: str) -> None
    async def history(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedList[Message]
```

### 4.5 Knowledge 模块

```python
class KnowledgeModule:
    async def list_datasets(self) -> list[Dataset]

    # 便捷方法：直接发起带 KB 检索的问答
    async def ask(
        self,
        question: str,
        dataset_ids: list[str],
        *,
        top_k: int = 5,
        score_threshold: float = 0.0,
        include_images: bool = False,
        session_id: str | None = None,
    ) -> ChatResponse
```

### 4.6 Images 模块

```python
class ImageModule:
    # 同步生图（适合快速场景，Gemini 优先）
    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,  # 自动路由
        size: str = "1024x1024",
        style: str | None = None,
        negative_prompt: str | None = None,
        session_id: str | None = None,
    ) -> ImageResult

    # 异步生图（适合高质量 / DashScope Wanx）
    async def generate_async(self, prompt: str, **kwargs) -> ImageTask

    # 轮询任务（SDK 自动轮询，可 await）
    async def wait_for_task(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        timeout: float = 120.0,
    ) -> ImageResult

    # 获取任务状态
    async def get_task(self, task_id: str) -> ImageTask
```

### 4.7 Artifacts 模块

```python
class ArtifactModule:
    async def create(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
        session_id: str | None = None,
    ) -> Artifact

    async def get(self, artifact_id: str) -> Artifact
    async def download(self, artifact_id: str) -> bytes
    async def download_url(self, artifact_id: str) -> str  # presigned URL
    async def list_by_session(self, session_id: str) -> list[Artifact]
    async def delete(self, artifact_id: str) -> None
```

### 4.8 Tools & MCP 模块

```python
class ToolModule:
    async def list_tools(self) -> list[ToolInfo]
    async def approve(self, approval_id: str, approved: bool) -> ApprovalResult

class MCPModule:
    """面向完整接入模式：注册自定义 MCP 服务器"""
    async def register_server(
        self,
        name: str,
        url: str,
        *,
        api_key: str | None = None,
        transport: str = "http",
        allowed_tools: list[str] | None = None,
    ) -> MCPServerInfo

    async def list_servers(self) -> list[MCPServerInfo]
    async def list_server_tools(self, server_name: str) -> list[ToolInfo]
```

### 4.9 Metrics 模块

```python
class MetricsModule:
    async def session_metrics(self, session_id: str) -> SessionMetrics
    async def tenant_metrics(
        self,
        hours: int = 24,  # 1-168
    ) -> TenantMetrics
```

---

## 5. SSE 流式方案（移动端封装）

### 5.1 问题分析

移动端直接处理 SSE 的痛点：手动解析 `data:` 行、处理 `\n\n` 分隔、重连逻辑、66 种事件类型的分发、网络切换时的恢复。

### 5.2 SDK 流式引擎设计

```
┌───────────────────────────────────────────┐
│              App Layer (UI)                │
│  on_text("Hello") → update TextView       │
│  on_tool_call({name: "search"}) → spinner  │
│  on_done() → hide loading                  │
└──────────────────┬────────────────────────┘
                   │ Callbacks / Delegates
┌──────────────────▼────────────────────────┐
│         SDK Stream Engine                  │
│  ┌──────────────────────────────────┐      │
│  │   Event Dispatcher               │      │
│  │   66 event types → typed events  │      │
│  └──────────┬───────────────────────┘      │
│  ┌──────────▼───────────────────────┐      │
│  │   SSE Parser                      │      │
│  │   data: {...}\n\n → StreamEvent   │      │
│  │   Handle [DONE] sentinel          │      │
│  └──────────┬───────────────────────┘      │
│  ┌──────────▼───────────────────────┐      │
│  │   Connection Manager              │      │
│  │   • Auto-reconnect (exp backoff) │      │
│  │   • Network change detection      │      │
│  │   • Timeout management            │      │
│  └──────────┬───────────────────────┘      │
│             │ HTTP/1.1 chunked transfer    │
└─────────────┼─────────────────────────────┘
              │
    ┌─────────▼──────────┐
    │ Assistant Service   │
    │  SSE: text/event-   │
    │  stream             │
    └─────────────────────┘
```

### 5.3 事件类型分层

SDK 将 66 种原始事件归类为 **6 个事件组**，开发者可选择订阅粒度：

| 事件组 | 包含事件 | 典型用途 |
|--------|---------|---------|
| **Content** | `text_delta`, `thinking_delta` | UI 文字渲染 |
| **Tools** | `tool_call`, `tool_result`, `approval_required` | 工具执行状态 |
| **RAG** | `context_retrieved`, `web_search_results`, `rag_evaluation` | 引用来源展示 |
| **Memory** | `memory_retrieved`, `memory_reflection_scheduled` | 记忆状态 |
| **System** | `session_created`, `usage`, `cache_metrics`, `finish`, `done` | 生命周期管理 |
| **Error** | `error` | 错误处理 |

### 5.4 移动端 SSE 封装策略

**Swift (iOS):**
- 基于 `URLSession` 的 data task + stream delegate
- SDK 提供 `AssistantStreamDelegate` protocol
- 支持 Combine `Publisher<StreamEvent, Error>` 和 async/await `AsyncStream`

**Kotlin (Android):**
- 基于 OkHttp `EventSource` (SSE 原生支持)
- SDK 提供 `AssistantStreamListener` interface
- 支持 Kotlin Flow `Flow<StreamEvent>` 和 coroutine suspend

**生成方式:** 通过 OpenAPI 3.1 Spec + openapi-generator 自动生成 Swift/Kotlin 基础代码，SDK 层手动封装 SSE 逻辑。

---

## 6. 认证封装设计

### 6.1 认证流程

```
┌─────────────┐         ┌──────────────┐         ┌────────────────┐
│  App Init   │         │  SDK Client  │         │  Assistant API  │
│             │         │              │         │                │
│  api_key    ├────────►│  Store in    │         │                │
│  tenant_id  │         │  AuthStore   │         │                │
│  base_url   │         │              │         │                │
└─────────────┘         └──────┬───────┘         └────────────────┘
                               │
    Every Request:             │
    ┌──────────────────────────▼────────────────────────────┐
    │  HTTP Interceptor (automatic)                         │
    │                                                       │
    │  headers["X-API-Key"]    = auth_store.api_key         │
    │  headers["X-Tenant-Id"]  = auth_store.tenant_id       │
    │  headers["Authorization"]= "Bearer " + jwt (if set)   │
    │  headers["X-Request-Id"] = uuid4()                    │
    │  headers["X-SDK-Version"]= "1.0.0"                    │
    └───────────────────────────────────────────────────────┘
```

### 6.2 后端认证兼容性

当前 `AuthMiddleware` 的优先级是 JWT → API Key → Guest → Anonymous。SDK 的 API Key 模式已被支持（`api_key_enabled: bool` in `AuthConfig`），但需要补充 **tenant_id 的显式传递**：

**后端需改造点：**

1. **新增 `X-Tenant-Id` header 解析**：当前 tenant_id 从 JWT claims 提取。API Key 模式下需从 header 读取，并与 API Key 绑定的 tenant 做校验。
2. **API Key → Tenant 映射表**：创建 `api_keys` 表，存储 `key_hash`, `tenant_id`, `tier`, `rate_limit`, `scopes`。
3. **Scope-based 权限**：API Key 可配置 `scopes`（如 `chat`, `kb`, `images`, `mcp`），限制 SDK 能访问的模块。

---

## 7. 三种接入模式

### 7.1 轻量模式（Lite）— 聊天 + 知识库问答

适用：快速集成智能客服、FAQ 问答。

**Python 示例（5 行初始化 + 5 行发消息 = 10 行）：**

```python
from ai_assistant import AssistantClient

# 初始化（5 行）
client = AssistantClient(
    api_key="wah_sk_xxxxx",
    tenant_id="wahda",
    base_url="https://ai.wahda.com/api/v1",
)

# 流式问答（5 行）
async for event in client.chat.stream(
    "What is the profit rate for a 12-month deposit?",
    kb_dataset_ids=["wahda-products"],
):
    if event.type == "text_delta":
        print(event.text, end="", flush=True)
```

**TypeScript 示例：**

```typescript
import { AssistantClient } from '@ai-assistant/sdk';

const client = new AssistantClient({
  apiKey: 'wah_sk_xxxxx',
  tenantId: 'wahda',
  baseUrl: 'https://ai.wahda.com/api/v1',
});

const stream = client.chat.stream({
  message: 'What is the profit rate for a 12-month deposit?',
  kbDatasetIds: ['wahda-products'],
});

for await (const event of stream) {
  if (event.type === 'text_delta') {
    process.stdout.write(event.text);
  }
}
```

### 7.2 标准模式（Standard）— 聊天 + KB + Skills + 生图

适用：全功能 AI 助手，含技能调用和生图。

```python
from ai_assistant import AssistantClient

client = AssistantClient(
    api_key="hlm_sk_xxxxx",
    tenant_id="halalmoney",
    base_url="https://ai.halalmoney.com/api/v1",
)

# 带技能的多轮对话
session = await client.sessions.create(metadata={"channel": "mobile"})

response = await client.chat.stream_with_handlers(
    "Help me analyze this month's spending and generate a summary chart",
    session_id=session.id,
    skills_enabled=True,
    enable_planning=True,
    on_text=lambda t: ui.append_text(t),
    on_tool_call=lambda tc: ui.show_tool_spinner(tc.name),
    on_tool_result=lambda tr: ui.hide_tool_spinner(),
    on_context=lambda ctx: ui.show_sources(ctx),
    on_done=lambda d: ui.mark_complete(),
)

# AI 生图
image = await client.images.generate(
    "A chart showing monthly spending breakdown, modern flat design",
    size="1024x1024",
    session_id=session.id,
)
ui.display_image(image.url)
```

### 7.3 完整模式（Full）— 所有能力 + 自定义 MCP 工具

适用：深度集成，自定义工具链。

```python
from ai_assistant import AssistantClient

client = AssistantClient(
    api_key="wah_sk_xxxxx",
    tenant_id="wahda",
    base_url="https://ai.wahda.com/api/v1",
)

# 注册自定义 MCP 服务器（Wahda 内部风控系统）
await client.mcp.register_server(
    name="wahda-risk",
    url="https://risk-api.wahda.internal/mcp",
    api_key="internal_key",
    allowed_tools=["check_transaction", "get_risk_score"],
)

# 完整 Agent Loop 对话
async for event in client.chat.stream(
    "Check if transaction TXN-2026-0403 has any risk flags, "
    "then search our compliance KB for relevant policies",
    kb_dataset_ids=["wahda-compliance"],
    execution_profile="power",
    skills_enabled=True,
    web_search=True,
):
    match event.type:
        case "text_delta":
            ui.append(event.text)
        case "tool_call":
            ui.show_tool(event.tool_name, event.arguments)
        case "tool_result":
            ui.show_result(event.tool_name, event.result)
        case "approval_required":
            approved = await ui.ask_user(event.tool_name, event.description)
            await client.tools.approve(event.approval_id, approved)
        case "context_retrieved":
            ui.show_citations(event.contexts)
        case "error":
            ui.show_error(event.message)
```

---

## 8. 数据模型定义

### 8.1 核心类型

```python
@dataclass
class ChatResponse:
    content: str
    usage: TokenUsage
    contexts: list[Context]       # KB 检索结果
    duration_ms: float
    model_id: str
    session_id: str | None
    run_id: str | None

@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

@dataclass
class StreamEvent:
    type: str                     # SSEEventType 值
    data: dict[str, Any]          # 事件负载
    timestamp: float

    # 便捷属性（按事件类型）
    @property
    def text(self) -> str | None:
        """text_delta / thinking_delta 的文本内容"""

    @property
    def tool_name(self) -> str | None:
        """tool_call / tool_result 的工具名"""

@dataclass
class Session:
    id: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any]
    message_count: int

@dataclass
class Message:
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: datetime
    tool_calls: list[ToolCall]
    citations: list[Citation]
    attachments: list[Attachment]

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    status: Literal["pending", "running", "completed", "failed"]

@dataclass
class Citation:
    dataset_id: str
    document_id: str
    score: float
    url: str | None
    preview: str

@dataclass
class ImageResult:
    url: str
    artifact_id: str
    model: str
    revised_prompt: str | None

@dataclass
class Dataset:
    id: str
    name: str
    description: str
    document_count: int

@dataclass
class Artifact:
    id: str
    type: Literal["image", "file", "document"]
    format: str
    filename: str
    size_bytes: int
    mime_type: str
    download_url: str
    created_at: datetime
```

---

## 9. 错误处理体系

```python
class AssistantError(Exception):
    """Base SDK error"""
    status_code: int
    error_code: str
    message: str
    request_id: str

class AuthenticationError(AssistantError): ...    # 401
class PermissionError(AssistantError): ...        # 403
class NotFoundError(AssistantError): ...          # 404
class RateLimitError(AssistantError): ...         # 429, 含 retry_after
class ValidationError(AssistantError): ...        # 422
class ServerError(AssistantError): ...            # 500+
class StreamError(AssistantError): ...            # SSE 连接断开
class TimeoutError(AssistantError): ...           # 请求超时

# 后端需统一错误响应格式：
# { "error": { "code": "rate_limit_exceeded", "message": "...", "request_id": "..." } }
```

**重试策略：**

| 错误类型 | 重试 | 策略 |
|---------|------|------|
| 429 Rate Limit | 是 | 等待 `Retry-After` header |
| 500+ Server Error | 是 | 指数退避，最多 3 次 |
| 网络错误 | 是 | 指数退避，最多 3 次 |
| 401/403/404/422 | 否 | 立即抛出 |
| SSE 断连 | 是 | 自动重连（最多 5 次） |

---

## 10. 用户研究分析

### 10.1 目标用户画像

| 维度 | Wahda 团队 | HalalMoney 团队 |
|------|-----------|----------------|
| 规模 | 3-5 移动开发 + 2 后端 | 2-3 移动开发 + 1 后端 |
| 技术栈 | Swift (iOS) + Kotlin (Android) | Flutter / React Native |
| AI 经验 | 低，首次集成 AI | 中，用过 OpenAI API |
| 核心场景 | 智能客服、合规文档分析 | 理财问答、消费分析生图 |
| 紧迫度 | Q2 2026 上线 | Q3 2026 上线 |

### 10.2 DX（开发者体验）目标

| 指标 | 目标值 | 说明 |
|------|-------|------|
| Time to First Message | < 15 分钟 | 从 `npm install` 到看到 AI 回复 |
| 初始化代码行数 | 5 行 | api_key + tenant_id + base_url |
| 发消息代码行数 | 5 行 | stream + event handler |
| 流式 UI 绑定 | 10 行 | 回调模式自动更新 UI |
| 文档覆盖率 | 100% | 每个公开方法都有示例 |

### 10.3 直接调 REST API 的痛点

1. **SSE 解析复杂**：移动端没有原生 SSE 库，需手动处理 chunked transfer encoding、`data:` 行解析、`\n\n` 分隔符、Unicode 流。估计每个平台需要 200-400 行 SSE 解析代码。

2. **Session 管理繁琐**：需要自行维护 session_id 生命周期、处理会话过期、管理本地 session 缓存与服务端同步。

3. **错误处理不统一**：HTTP 错误、SSE 错误、业务错误混杂，没有统一的错误码体系，移动端需要写大量 switch-case。

4. **认证 header 重复**：每个请求都要手动拼接 `X-API-Key`、`X-Tenant-Id`，容易遗漏。

5. **66 种 SSE 事件**：不知道哪些要处理、哪些可以忽略，缺乏分层指导。

6. **重试与重连**：没有内建的指数退避、SSE 断线重连、网络切换恢复。

### 10.4 SDK 解决方案映射

| 痛点 | SDK 方案 |
|------|---------|
| SSE 解析 | 内建 SSE Parser + 类型化事件 |
| Session 管理 | `SessionModule` 自动创建/复用/清理 |
| 错误处理 | 统一异常层级 + 自动重试 |
| 认证 header | HTTP 拦截器自动注入 |
| 事件分类 | 6 个事件组 + 回调模式 |
| 重连 | 自动指数退避重连 |

---

## 11. 后端改造清单

### 11.1 必须改造（Phase 1 前置）

| # | 改造项 | 文件 | 说明 |
|---|--------|------|------|
| 1 | **API Key + Tenant 绑定表** | 新建 `src/core/auth/api_key_store.py` | 创建 `api_keys` 表：`key_hash`, `tenant_id`, `tier`, `scopes`, `rate_limit`, `created_at`, `expires_at` |
| 2 | **X-Tenant-Id header 支持** | `src/core/middleware/auth.py` | API Key 模式下从 header 读取 tenant_id，与 key 绑定的 tenant 校验 |
| 3 | **统一错误响应格式** | `src/api/v1/assistant.py` | 所有端点返回 `{"error": {"code": "...", "message": "...", "request_id": "..."}}` |
| 4 | **Rate Limit 中间件** | 新建 `src/core/middleware/rate_limit.py` | 基于 API Key 的令牌桶限流，返回 `429` + `Retry-After` header |
| 5 | **CORS 配置** | `src/core/middleware/` | 允许 SDK 域名的跨域请求（Web SDK 场景） |

### 11.2 建议改造（Phase 2）

| # | 改造项 | 文件 | 说明 |
|---|--------|------|------|
| 6 | **OpenAPI 3.1 Spec 自动生成** | 配置 FastAPI OpenAPI export | 当前 schema 已用 Pydantic，需确保所有端点都有 `response_model` |
| 7 | **MCP 服务器动态注册 API** | 新建 `/assistant/mcp/servers` 端点 | 当前 MCP 配置从 YAML 文件加载，需支持 API 动态注册（按 tenant 隔离） |
| 8 | **Webhook 回调支持** | 新建 `/assistant/webhooks` | 异步任务完成时推送通知（替代轮询） |
| 9 | **SDK 版本兼容检查** | `src/core/middleware/` | 读取 `X-SDK-Version` header，返回弃用警告 |

### 11.3 可选改造（Phase 3）

| # | 改造项 | 说明 |
|---|--------|------|
| 10 | **WebSocket 传输** | SSE 的替代方案，支持双向通信（tool approval 不需要额外 HTTP 请求） |
| 11 | **GraphQL 端点** | 移动端按需取字段，减少数据传输 |
| 12 | **SDK Telemetry 端点** | 收集 SDK 使用数据（匿名），优化 DX |

---

## 12. 实施路线图

### Phase 1：核心 SDK（4 周）— 优先级 P0

**目标：** 轻量模式可用，支持流式聊天 + KB 问答。

| 周 | 任务 |
|----|------|
| W1 | 后端改造 #1-#5；SDK 骨架（auth, transport, models） |
| W2 | Chat 模块（send + stream）；SSE Parser + Reconnect |
| W3 | Session 模块；Knowledge 模块；错误体系 |
| W4 | Python SDK 打包发布；TypeScript SDK 同步；OpenAPI Spec 导出；集成测试 |

**交付物：**
- `ai-assistant-sdk` Python package (PyPI)
- `@ai-assistant/sdk` TypeScript package (npm)
- OpenAPI 3.1 YAML spec
- 快速入门文档 + 轻量模式示例

### Phase 2：标准能力（3 周）— 优先级 P1

**目标：** 标准模式可用，支持 Skills + ImageGen + Artifacts。

| 周 | 任务 |
|----|------|
| W5 | Images 模块（同步 + 异步 + 轮询）；Artifacts 模块 |
| W6 | Skills 集成；Tools 模块（approval workflow） |
| W7 | 后端改造 #6-#9；SDK 文档完善；Wahda/HalalMoney 对接 |

**交付物：**
- SDK v1.1 with Images + Skills + Artifacts
- Swift/Kotlin 代码通过 OpenAPI 生成 + SSE 手动封装
- Wahda / HalalMoney 集成指南

### Phase 3：完整模式 + 生态（3 周）— 优先级 P2

**目标：** 完整模式可用，MCP 自定义工具链。

| 周 | 任务 |
|----|------|
| W8 | MCP 动态注册 API + SDK 封装；Metrics 模块 |
| W9 | Webhook 回调支持；高级流式功能（Agent Loop 事件） |
| W10 | 端到端测试；性能基准测试；SDK v2.0 发布 |

**交付物：**
- SDK v2.0 with full MCP + Metrics
- 性能报告（延迟、吞吐量）
- 完整模式集成示例

---

## 13. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| SSE 在弱网环境断连频繁 | 移动端体验差 | SDK 内建 3 级重连策略 + 消息 ID 去重 |
| 66 种事件类型变更 | SDK 兼容性断裂 | 版本化事件 schema + 未知事件静默忽略 |
| API Key 泄露 | 安全风险 | Key rotation API + scope 限制 + 客户端 Key 仅限特定 scope |
| 后端改造阻塞 SDK 开发 | 延期 | Phase 1 改造项最小化，SDK 可 mock 后端先行开发 |
| Swift/Kotlin 代码生成质量 | DX 差 | 自动生成基础代码 + 手动优化 SSE 层和类型安全 |

---

## 14. 成功指标

| 指标 | Phase 1 目标 | Phase 3 目标 |
|------|-------------|-------------|
| Time to First Message | < 15 min | < 10 min |
| 集成代码量（基本聊天） | < 20 行 | < 15 行 |
| SSE 首 token 延迟 | < 800ms | < 500ms |
| 流式断连恢复率 | > 95% | > 99% |
| SDK 接入 App 数 | 2 (Wahda + HalalMoney) | 5+ |
| 开发者满意度 (NPS) | > 40 | > 60 |

---

## 附录 A：SSE 事件类型完整列表

| 事件组 | 事件名 | 说明 |
|--------|--------|------|
| Content | `text_delta` | 文本增量 |
| Content | `thinking_delta` | 思考过程增量 |
| Tools | `tool_call` | 工具调用开始 |
| Tools | `tool_call_start` | 工具调用详情 |
| Tools | `tool_call_end` | 工具调用结束 |
| Tools | `tool_result` | 工具返回结果 |
| RAG | `context_retrieved` | KB 检索结果 |
| RAG | `web_search_results` | 网页搜索结果 |
| RAG | `rag_evaluation` | RAG 质量评估 |
| RAG | `context_budget` | Token 预算分配 |
| RAG | `context_compacted` | 上下文压缩事件 |
| RAG | `context_detail` | Token 明细 |
| Memory | `memory_retrieved` | 记忆召回 |
| Memory | `memory_reflection_scheduled` | 反思任务调度 |
| Gateway | `queue_state` | 命令队列状态 |
| Gateway | `queue_steered` | 队列方向引导 |
| Gateway | `approval_required` | 需要用户审批 |
| Gateway | `approval_result` | 审批结果 |
| Gateway | `gateway_decision` | 网关决策 |
| Gateway | `sandbox_decision` | 沙盒决策 |
| Skills | `skill_selected` | 技能选中 |
| Skills | `skill_loaded` | 技能加载完成 |
| Skills | `skill_create_pending_approval` | 技能创建待审批 |
| System | `session_created` | 会话创建 |
| System | `session_updated` | 会话更新 |
| System | `cache_metrics` | KV 缓存指标 |
| System | `usage` | Token 使用统计 |
| System | `run_started` | Run 开始 |
| System | `finish` | 生成结束 |
| System | `done` | 响应完成 |
| Error | `error` | 错误事件 |
