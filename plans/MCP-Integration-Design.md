# MCP Integration Design — Universal Agent Connector Layer

> **Version**: 1.0
> **Date**: 2026-04-01
> **Purpose**: 让 AI Assistant 通过 MCP 协议实现对任意业务系统的即插即用集成
> **Target Scenarios**: Wahda 社交操作, Halal Money 智能客服, 未来所有 Hejaz 业务线

---

## 1. Architecture: Agent 通用性四层模型

```
┌───────────────────────────────────────────────────────────────────┐
│                    AI Assistant (Universal Agent)                  │
│                                                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────┐  │
│  │ KB Layer    │  │ Skills Layer│  │ Tools Layer │  │ MCP    │  │
│  │ (通用知识)   │  │ (通用技能)   │  │ (内建工具)   │  │ Layer  │  │
│  │             │  │             │  │             │  │(外部系统)│  │
│  │ ✅ Done     │  │ ⚠️ Phase 2  │  │ ✅ Done     │  │❌ TODO │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  └────────┘  │
│                                                                   │
│  Agent Loop (8-Step) ←── 所有层的能力在这里统一编排                   │
└───────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
   Islamic Finance KB    sales-quiz skill    MCP Protocol (JSON-RPC)
   Product Docs KB       doc-generator       ┌──────────────────────┐
   Compliance KB         skill-create        │  wahda-mcp-server    │
                                             │  halalmoney-mcp      │
                                             │  crm-mcp-server      │
                                             │  任何未来系统...       │
                                             └──────────────────────┘
```

---

## 2. MCP Client Adapter Design

### 2.1 Core: MCPClient

```python
"""
MCP Client — JSON-RPC 2.0 client implementing Model Context Protocol.

Supports:
- tools/list: Discover available tools from MCP server
- tools/call: Invoke a tool on the MCP server
- resources/list, resources/read: Access server resources (optional)
- prompts/list, prompts/get: Get prompt templates (optional)

Transport: HTTP+SSE (Streamable HTTP) — chosen over stdio for
           server-to-server deployment (no subprocess management).
"""

@dataclass
class MCPServerConfig:
    """Configuration for connecting to an MCP server."""
    name: str                          # Human-readable name (e.g., "wahda")
    url: str                           # Server endpoint (e.g., "http://wahda-mcp:3000")
    api_key: str | None = None         # Auth token
    transport: str = "http"            # "http" | "stdio" (http for server-side)
    timeout: float = 30.0
    enabled: bool = True
    description: str = ""              # What this server provides
    allowed_tools: list[str] | None = None   # Whitelist (None = all)
    blocked_tools: list[str] | None = None   # Blacklist
    max_concurrent: int = 10           # Concurrency limit per server

@dataclass
class MCPTool:
    """Tool definition discovered from MCP server."""
    name: str                          # e.g., "create_post"
    description: str
    input_schema: dict                 # JSON Schema for parameters
    server_name: str                   # Which MCP server provides this
    full_name: str = ""                # "{server_name}:{tool_name}"

@dataclass
class MCPToolResult:
    """Result from MCP tool invocation."""
    content: list[dict]                # [{type: "text", text: "..."}, ...]
    is_error: bool = False


class MCPClient:
    """MCP protocol client for a single server."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._http: httpx.AsyncClient | None = None
        self._tools: list[MCPTool] = []
        self._initialized: bool = False

    async def initialize(self) -> None:
        """MCP handshake: send initialize request, receive server capabilities."""
        self._http = httpx.AsyncClient(
            base_url=self.config.url,
            timeout=self.config.timeout,
            headers={"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {},
        )
        # JSON-RPC: initialize
        resp = await self._jsonrpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "hejaz-ai-agent", "version": "1.0.0"},
        })
        # Send initialized notification
        await self._notify("notifications/initialized")
        self._initialized = True

    async def list_tools(self) -> list[MCPTool]:
        """Discover tools from MCP server (tools/list)."""
        resp = await self._jsonrpc("tools/list", {})
        self._tools = [
            MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.config.name,
                full_name=f"{self.config.name}:{t['name']}",
            )
            for t in resp.get("tools", [])
            if self._is_tool_allowed(t["name"])
        ]
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolResult:
        """Invoke a tool on the MCP server (tools/call)."""
        resp = await self._jsonrpc("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return MCPToolResult(
            content=resp.get("content", []),
            is_error=resp.get("isError", False),
        )

    async def _jsonrpc(self, method: str, params: dict) -> dict:
        """Send JSON-RPC 2.0 request."""
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        resp = await self._http.post("/mcp", json=payload)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise MCPError(result["error"]["code"], result["error"]["message"])
        return result.get("result", {})

    def _is_tool_allowed(self, tool_name: str) -> bool:
        """Check against whitelist/blacklist."""
        if self.config.blocked_tools and tool_name in self.config.blocked_tools:
            return False
        if self.config.allowed_tools is not None:
            return tool_name in self.config.allowed_tools
        return True
```

### 2.2 MCPManager: Multi-Server Orchestration

```python
class MCPManager:
    """Manage multiple MCP server connections and tool registration."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        configs: list[MCPServerConfig] | None = None,
    ):
        self.tool_registry = tool_registry
        self._clients: dict[str, MCPClient] = {}
        self._configs = configs or []

    async def initialize_all(self) -> dict[str, int]:
        """Connect to all configured MCP servers and discover tools."""
        results = {}
        for config in self._configs:
            if not config.enabled:
                continue
            try:
                client = MCPClient(config)
                await client.initialize()
                tools = await client.list_tools()
                self._clients[config.name] = client

                # Register each MCP tool into the agent's ToolRegistry
                for mcp_tool in tools:
                    self._register_mcp_tool(mcp_tool, client)

                results[config.name] = len(tools)
                logger.info(f"MCP server '{config.name}': {len(tools)} tools registered")
            except Exception as e:
                logger.error(f"MCP server '{config.name}' failed: {e}")
                results[config.name] = -1

        return results

    def _register_mcp_tool(self, mcp_tool: MCPTool, client: MCPClient):
        """Register an MCP tool as a callable tool in ToolRegistry."""
        definition = ToolDefinition(
            name=f"mcp_{mcp_tool.full_name}",          # e.g., "mcp_wahda:create_post"
            description=f"[{mcp_tool.server_name}] {mcp_tool.description}",
            parameters=mcp_tool.input_schema,
            category=ToolCategory.MCP,                  # New category
            risk_level=ToolRiskLevel.MEDIUM,             # Default; can override per-server
            when_to_use=mcp_tool.description,
        )

        async def executor(request: ToolCallRequest) -> ToolCallResult:
            result = await client.call_tool(mcp_tool.name, request.arguments)
            text_parts = [c["text"] for c in result.content if c.get("type") == "text"]
            return ToolCallResult(
                success=not result.is_error,
                result="\n".join(text_parts),
            )

        self.tool_registry.register(definition, executor)

    async def refresh_tools(self, server_name: str | None = None):
        """Re-discover tools (for hot-reload without restart)."""
        targets = [server_name] if server_name else list(self._clients.keys())
        for name in targets:
            if name in self._clients:
                tools = await self._clients[name].list_tools()
                for t in tools:
                    self._register_mcp_tool(t, self._clients[name])

    async def shutdown(self):
        """Close all MCP connections."""
        for client in self._clients.values():
            await client.close()
```

### 2.3 Configuration (YAML/Environment)

```yaml
# config/mcp_servers.yaml
mcp_servers:
  - name: wahda
    url: http://wahda-mcp:3000
    api_key: ${WAHDA_MCP_API_KEY}
    description: "Wahda social platform — posts, messages, search"
    enabled: true
    max_concurrent: 10

  - name: halalmoney
    url: http://halalmoney-mcp:3000
    api_key: ${HALALMONEY_MCP_API_KEY}
    description: "Halal Money — portfolio, products, compliance checks"
    enabled: true
    allowed_tools:                   # Whitelist for safety
      - get_portfolio
      - get_products
      - check_halal_status
      - get_transactions
    blocked_tools:
      - delete_account               # Never allow via AI
      - transfer_funds               # Requires human approval

  - name: crm
    url: http://crm-mcp:3000
    description: "Internal CRM system"
    enabled: false                    # Not yet deployed
```

---

## 3. Agent Loop Integration

### 3.1 MCP 接入 Agent Loop Step 5-6

```python
# In agent_loop.py — Step 5 (Context Building)

async def _step5_context_building(self, ctx: ExecutionContext):
    # ... existing KB retrieval, memory loading, skills loading ...

    # NEW: Register MCP tools for this session
    if self.mcp_manager and ctx.config.mcp_enabled:
        # MCP tools are already registered in ToolRegistry at startup
        # But we can dynamically filter based on session/tenant context
        mcp_tool_names = [
            t.name for t in self.tool_registry.list()
            if t.category == ToolCategory.MCP
        ]
        if mcp_tool_names:
            yield self._event("status", phase="context",
                data=f"MCP tools available: {', '.join(mcp_tool_names)}")

# In agent_loop.py — Step 6 (ReAct Execution)
# MCP tools are called exactly like any other tool — no special handling needed.
# The ToolRegistry abstracts away the difference between builtin, skill, and MCP tools.
```

### 3.2 Tool 统一视图 (LLM 视角)

```json
// What the LLM sees — all tools look the same regardless of source
{
  "tools": [
    // Builtin tools
    {"name": "kb_search", "description": "Search knowledge base..."},
    {"name": "web_search", "description": "Search the web..."},

    // Skill tools
    {"name": "skill_quiz_generator", "description": "[Skill] Generate quiz..."},

    // MCP tools (dynamically discovered)
    {"name": "mcp_wahda:create_post", "description": "[wahda] Create a post in Wahda"},
    {"name": "mcp_wahda:search_messages", "description": "[wahda] Search messages..."},
    {"name": "mcp_wahda:send_message", "description": "[wahda] Send a message..."},
    {"name": "mcp_halalmoney:get_portfolio", "description": "[halalmoney] Get user portfolio"},
    {"name": "mcp_halalmoney:check_halal_status", "description": "[halalmoney] Check if investment is halal"}
  ]
}
```

LLM 不知道也不需要知道一个 tool 是内建的、skill 的、还是 MCP 的。统一的 ToolRegistry 抽象让所有 tool 对 LLM 完全平等。

---

## 4. MCP Server Templates (Wahda & Halal Money)

### 4.1 Wahda MCP Server

```
wahda-mcp-server/
├── package.json
├── src/
│   ├── index.ts                   # MCP server entry
│   ├── tools/
│   │   ├── create_post.ts         # Create a post/status update
│   │   ├── search_messages.ts     # Search chat messages
│   │   ├── send_message.ts        # Send direct message
│   │   ├── get_contacts.ts        # List user contacts
│   │   ├── get_groups.ts          # List groups/channels
│   │   ├── search_posts.ts        # Search public posts
│   │   └── get_notifications.ts   # Get recent notifications
│   └── wahda-api-client.ts        # Wahda backend API wrapper
└── Dockerfile
```

Tool definitions:
```typescript
// wahda:create_post
{
  name: "create_post",
  description: "Create a new post in Wahda social platform",
  inputSchema: {
    type: "object",
    properties: {
      content: { type: "string", description: "Post text content" },
      group_id: { type: "string", description: "Target group ID (optional)" },
      media_urls: { type: "array", items: { type: "string" }, description: "Attached media URLs" },
      visibility: { type: "string", enum: ["public", "contacts", "group"], default: "public" }
    },
    required: ["content"]
  }
}

// wahda:search_messages
{
  name: "search_messages",
  description: "Search messages across chats and groups in Wahda",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query" },
      group_id: { type: "string", description: "Limit to specific group" },
      from_user: { type: "string", description: "Filter by sender" },
      date_from: { type: "string", description: "Start date (ISO 8601)" },
      date_to: { type: "string", description: "End date (ISO 8601)" },
      limit: { type: "integer", default: 20, maximum: 50 }
    },
    required: ["query"]
  }
}

// wahda:send_message
{
  name: "send_message",
  description: "Send a direct message or group message in Wahda",
  inputSchema: {
    type: "object",
    properties: {
      recipient_id: { type: "string", description: "User or group ID" },
      content: { type: "string", description: "Message text" },
      reply_to: { type: "string", description: "Message ID to reply to" }
    },
    required: ["recipient_id", "content"]
  }
}
```

### 4.2 Halal Money MCP Server

```
halalmoney-mcp-server/
├── package.json
├── src/
│   ├── index.ts
│   ├── tools/
│   │   ├── get_portfolio.ts        # Get user's investment portfolio
│   │   ├── get_products.ts         # List available halal products
│   │   ├── check_halal_status.ts   # Check if specific investment is halal
│   │   ├── get_transactions.ts     # Recent transaction history
│   │   ├── get_super_balance.ts    # Superannuation balance
│   │   ├── get_market_data.ts      # Halal ETF/fund market data
│   │   └── get_faqs.ts             # Product FAQs
│   ├── resources/
│   │   ├── product_catalog.ts      # Product info as MCP resource
│   │   └── compliance_rules.ts     # Sharia compliance rules
│   └── halalmoney-api-client.ts
└── Dockerfile
```

---

## 5. Scenario Walkthrough

### 5.1 Wahda: "帮我在群里发个帖子说明天聚餐"

```
User: "帮我在 Wahda 的 Sydney Muslim Youth 群发个帖子说明天 7 点在 Lakemba 聚餐"

Agent Loop:
  Step 2: Scenario = social_action (detect Wahda + 发帖 intent)
  Step 5: Load MCP tools → sees mcp_wahda:create_post
  Step 6: ReAct Execution
    → LLM calls: mcp_wahda:create_post({
        content: "明天晚上7点在 Lakemba 聚餐，欢迎大家参加！📍",
        group_id: "sydney_muslim_youth",
        visibility: "group"
      })
    → MCPManager routes to wahda MCP server
    → Wahda MCP server calls Wahda backend API
    → Returns: {type: "text", text: "Post created successfully (ID: post_123)"}
  Step 8: "已经帮你在 Sydney Muslim Youth 群发了聚餐帖子 ✅"
```

### 5.2 Halal Money: "我的投资组合是不是 halal 的？"

```
User: "Check if my current portfolio is fully halal compliant"

Agent Loop:
  Step 2: Scenario = finance_query (detect halal + portfolio intent)
  Step 4: RAG from Islamic Finance KB (compliance rules)
  Step 5: Load MCP tools → sees mcp_halalmoney:get_portfolio, mcp_halalmoney:check_halal_status
  Step 6: ReAct Execution
    → Turn 1: LLM calls mcp_halalmoney:get_portfolio({user_id: "current"})
      → Returns: [{fund: "Wahed FTSE USA", allocation: 40%}, {fund: "IRESS", allocation: 30%}, ...]
    → Turn 2: LLM calls mcp_halalmoney:check_halal_status({fund_name: "IRESS"})
      → Returns: {halal: false, reason: "Revenue from interest exceeds 5% threshold"}
    → Turn 3: LLM generates response with RAG context (Sharia rules) + tool results
  Step 8: "Your portfolio is 70% halal compliant. IRESS (30% allocation) does not meet
          Sharia compliance because... Based on our Islamic Finance guidelines, you might
          consider replacing it with [alternative from KB]."
```

---

## 6. Implementation Phases

### Phase 1: MCP Client Core (3-4 days)
- [ ] `src/services/assistant/mcp/client.py` — MCPClient (initialize, tools/list, tools/call)
- [ ] `src/services/assistant/mcp/manager.py` — MCPManager (multi-server, tool registration)
- [ ] `src/services/assistant/mcp/config.py` — MCPServerConfig, YAML loader
- [ ] `src/services/assistant/mcp/errors.py` — MCPError handling
- [ ] Integrate MCPManager into `agent_loop.py` Step 5-6
- [ ] Add `ToolCategory.MCP` to tool_registry.py
- [ ] Config: `config/mcp_servers.yaml`
- [ ] Unit tests for MCPClient mock server

### Phase 2: Wahda MCP Server (2-3 days)
- [ ] `wahda-mcp-server/` — Node.js MCP server scaffold
- [ ] Implement tools: create_post, search_messages, send_message, get_contacts, search_posts
- [ ] Wahda backend API client wrapper
- [ ] Dockerfile + docker-compose integration
- [ ] E2E test: Agent → MCP → Wahda mock API

### Phase 3: Halal Money MCP Server (2-3 days)
- [ ] `halalmoney-mcp-server/` — Node.js MCP server scaffold
- [ ] Implement tools: get_portfolio, get_products, check_halal_status, get_transactions
- [ ] Implement resources: product_catalog, compliance_rules
- [ ] Security: tool whitelist/blacklist enforcement
- [ ] E2E test: Agent → MCP → Halal Money mock API

### Phase 4: Management & Observability (1-2 days)
- [ ] API endpoints: `GET /api/v1/assistant/mcp/servers` (list connected servers)
- [ ] API endpoints: `GET /api/v1/assistant/mcp/tools` (list all MCP tools)
- [ ] API endpoints: `POST /api/v1/assistant/mcp/servers/:name/refresh` (hot-reload tools)
- [ ] MCP tool invocation metrics (latency, success rate, error rate)
- [ ] Frontend: MCP server status widget in admin dashboard

---

## 7. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| MCP server impersonation | API key auth on every connection; TLS in production |
| Dangerous tool exposure | Per-server `allowed_tools` / `blocked_tools` whitelist/blacklist |
| Data leakage via MCP | PII filter applied to tool arguments before sending |
| Runaway MCP calls | Per-server `max_concurrent` semaphore; per-tool timeout |
| Financial operations | `transfer_funds`, `delete_account` permanently blocked; `buy`/`sell` require human approval via ToolPolicyLattice |
| Tool injection | MCP tool names prefixed with `mcp_{server}:` to prevent collision with builtin tools |

---

## 8. Trade-off Analysis

| Decision | Choice | Trade-off |
|----------|--------|-----------|
| Transport | HTTP+SSE (not stdio) | 适合 server-to-server; 不支持 local CLI MCP servers — 可后续加 stdio transport |
| Tool registration | Startup discovery + hot-reload API | 简单; 不支持 tool 变更推送 — 可后续加 SSE notification |
| Server config | YAML file + env vars | 简单部署; 不支持运行时动态添加 — 可后续加 DB-backed config |
| MCP server language | Node.js (TypeScript) | MCP SDK 生态最成熟; Python SDK 也可用 |
| Tool naming | `mcp_{server}:{tool}` | 清晰来源; tool name 较长 — LLM 能处理 |
