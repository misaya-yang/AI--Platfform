"""
MCP Client — JSON-RPC 2.0 client implementing Model Context Protocol.

Transport: HTTP (Streamable HTTP) for server-to-server deployment.
Spec: https://modelcontextprotocol.io/specification/2025-11-25

Security: Tool descriptions from MCP servers are UNTRUSTED.
Input validation is required before passing to LLM.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Limits to prevent abuse from malicious MCP servers
MAX_TOOLS_PER_SERVER = 50
MAX_DESCRIPTION_LENGTH = 500
MAX_TOOL_NAME_LENGTH = 64


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
        self._semaphore = asyncio.Semaphore(config.max_concurrent)

    @staticmethod
    def _validate_url(url: str) -> None:
        """Block private/loopback IPs and non-HTTPS URLs (except localhost for dev)."""
        import ipaddress
        import socket
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""

        # Allow localhost and Docker-internal networks for dev/deployment
        if hostname in ("localhost", "127.0.0.1") or hostname.endswith(".internal"):
            return

        # Allow Docker Compose service names (resolve to 172.x private IPs)
        # In production, MCP servers run as Docker containers on the same network
        import os
        if os.environ.get("DOCKER_NETWORK_ALLOW_PRIVATE", "true").lower() == "true":
            return

        # Resolve hostname and block private IPs
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(hostname))
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ValueError(f"MCP URL resolves to private IP: {ip}")
        except socket.gaierror:
            pass  # DNS may not resolve in all environments

        # Warn if not HTTPS for non-local URLs
        if parsed.scheme != "https" and not hostname.startswith("localhost"):
            logger.warning(f"MCP server '{hostname}' uses HTTP — credentials may be transmitted in cleartext")

    async def initialize(self) -> dict:
        """MCP handshake: initialize → notifications/initialized."""
        self._validate_url(self.config.url)

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
        await self._notify("notifications/initialized")
        self._initialized = True
        logger.info(f"MCP '{self.config.name}' initialized: {self._server_info.get('name', '?')}")
        return result

    async def list_tools(self) -> list[MCPTool]:
        """Discover tools from MCP server (tools/list)."""
        resp = await self._jsonrpc("tools/list", {})
        raw_tools = resp.get("tools", [])[:MAX_TOOLS_PER_SERVER]
        self._tools = [
            MCPTool(
                name=self._sanitize_name(t["name"]),
                description=self._sanitize_description(t.get("description", "")),
                input_schema=t.get("inputSchema", {}),
                server_name=self.config.name,
            )
            for t in raw_tools
            if self._is_tool_allowed(t.get("name", ""))
        ]
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolResult:
        """Invoke a tool on the MCP server (tools/call) with concurrency limiting."""
        async with self._semaphore:
            start = time.monotonic()
            try:
                resp = await self._jsonrpc("tools/call", {
                    "name": tool_name,
                    "arguments": arguments,
                })
                duration = (time.monotonic() - start) * 1000
                logger.info(f"MCP tool '{self.config.name}:{tool_name}' completed in {duration:.0f}ms")
                return MCPToolResult(
                    content=resp.get("content", []),
                    is_error=resp.get("isError", False),
                )
            except Exception as e:
                duration = (time.monotonic() - start) * 1000
                logger.error(f"MCP tool '{self.config.name}:{tool_name}' failed in {duration:.0f}ms: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Tool call failed: {e}"}],
                    is_error=True,
                )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        self._initialized = False

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def _jsonrpc(self, method: str, params: dict) -> dict:
        """Send JSON-RPC 2.0 request with error handling."""
        if not self._http:
            raise MCPError(-1, "Client not connected")
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        try:
            resp = await self._http.post("/mcp", json=payload)
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise MCPError(-2, f"Request to {self.config.name} timed out")
        except httpx.HTTPStatusError as e:
            raise MCPError(e.response.status_code, f"HTTP {e.response.status_code} from {self.config.name}")
        except httpx.ConnectError:
            raise MCPError(-3, f"Cannot connect to {self.config.name} at {self.config.url}")

        result = resp.json()
        if "error" in result:
            err = result["error"]
            raise MCPError(err.get("code", -1), err.get("message", "Unknown error"))
        return result.get("result", {})

    async def _notify(self, method: str, params: dict | None = None) -> None:
        """Send JSON-RPC 2.0 notification (fire-and-forget)."""
        if not self._http:
            return
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            await self._http.post("/mcp", json=payload)
        except Exception:
            pass

    def _is_tool_allowed(self, tool_name: str) -> bool:
        if not tool_name:
            return False
        if self.config.blocked_tools and tool_name in self.config.blocked_tools:
            return False
        if self.config.allowed_tools is not None:
            return tool_name in self.config.allowed_tools
        return True

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize tool name — prevent injection via malicious server."""
        import re
        clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:MAX_TOOL_NAME_LENGTH]
        return clean or "unnamed"

    @staticmethod
    def _sanitize_description(desc: str) -> str:
        """Truncate and sanitize tool description — untrusted content from MCP server."""
        return desc[:MAX_DESCRIPTION_LENGTH].replace("\n", " ").strip()
