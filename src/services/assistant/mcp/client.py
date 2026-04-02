"""
MCP Client — JSON-RPC 2.0 client implementing Model Context Protocol.

Transport: HTTP (Streamable HTTP) for server-to-server deployment.
Spec: https://modelcontextprotocol.io/specification/2025-11-25
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class MCPError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(f"MCP Error {code}: {message}")


@dataclass
class MCPServerConfig:
    """Configuration for connecting to an MCP server."""
    name: str
    url: str
    api_key: str | None = None
    transport: str = "http"
    timeout: float = 30.0
    enabled: bool = True
    description: str = ""
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] | None = None
    max_concurrent: int = 10


@dataclass
class MCPTool:
    """Tool definition discovered from MCP server."""
    name: str
    description: str
    input_schema: dict
    server_name: str
    full_name: str = ""

    def __post_init__(self):
        if not self.full_name:
            self.full_name = f"{self.server_name}:{self.name}"


@dataclass
class MCPToolResult:
    """Result from MCP tool invocation."""
    content: list[dict] = field(default_factory=list)
    is_error: bool = False


class MCPClient:
    """MCP protocol client for a single server (HTTP transport)."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._http: httpx.AsyncClient | None = None
        self._tools: list[MCPTool] = []
        self._initialized: bool = False
        self._server_info: dict = {}

    async def initialize(self) -> dict:
        """MCP handshake: initialize → notifications/initialized."""
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        self._http = httpx.AsyncClient(
            base_url=self.config.url,
            timeout=self.config.timeout,
            headers=headers,
        )

        result = await self._jsonrpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "hejaz-ai-gateway", "version": "1.0.0"},
        })
        self._server_info = result.get("serverInfo", {})

        # Send initialized notification
        await self._notify("notifications/initialized")
        self._initialized = True
        logger.info(f"MCP '{self.config.name}' initialized: {self._server_info.get('name', '?')}")
        return result

    async def list_tools(self) -> list[MCPTool]:
        """Discover tools from MCP server (tools/list)."""
        resp = await self._jsonrpc("tools/list", {})
        self._tools = [
            MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.config.name,
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

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def _jsonrpc(self, method: str, params: dict) -> dict:
        """Send JSON-RPC 2.0 request."""
        if not self._http:
            raise MCPError(-1, "Client not connected")
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
            err = result["error"]
            raise MCPError(err.get("code", -1), err.get("message", "Unknown error"))
        return result.get("result", {})

    async def _notify(self, method: str, params: dict | None = None) -> None:
        """Send JSON-RPC 2.0 notification (no id, no response expected)."""
        if not self._http:
            return
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            await self._http.post("/mcp", json=payload)
        except Exception:
            pass  # Notifications are fire-and-forget

    def _is_tool_allowed(self, tool_name: str) -> bool:
        if self.config.blocked_tools and tool_name in self.config.blocked_tools:
            return False
        if self.config.allowed_tools is not None:
            return tool_name in self.config.allowed_tools
        return True
