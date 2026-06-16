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

    # SSRF-sensitive IP ranges that MUST be blocked even in allow-private mode.
    # These would leak cloud IAM credentials or internal infrastructure.
    _BLOCKED_CIDRS = (
        "169.254.0.0/16",   # Link-local (AWS/GCP/Azure metadata, 169.254.169.254)
        "100.64.0.0/10",    # Carrier-grade NAT
        "::1/128",          # IPv6 loopback (still blocked when HTTP, see below)
        "fe80::/10",        # IPv6 link-local
        "fc00::/7",         # IPv6 ULA
    )

    @staticmethod
    def _is_local_hostname(hostname: str) -> bool:
        """True for localhost/loopback/docker-internal names that are safe in dev."""
        return (
            hostname in ("localhost", "127.0.0.1", "::1")
            or hostname.endswith(".internal")
            or hostname.endswith(".local")
        )

    @classmethod
    def _validate_url(cls, url: str) -> None:
        """Validate MCP URL against SSRF and cleartext-credential risks.

        Rules:
        - scheme must be http or https
        - localhost / docker-internal names are always allowed
        - 169.254.0.0/16 (cloud metadata) is ALWAYS blocked, even in allow-private mode
        - Other private IPs allowed only if DOCKER_NETWORK_ALLOW_PRIVATE=true (production)
        - Non-HTTPS to non-local hosts is rejected (prevents cleartext credential leak)
        """
        import ipaddress
        import os
        import socket
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        hostname = parsed.hostname or ""

        if scheme not in ("http", "https"):
            raise ValueError(f"MCP URL must use http:// or https://, got {scheme!r}")
        if not hostname:
            raise ValueError("MCP URL missing hostname")

        is_local = cls._is_local_hostname(hostname)

        # Non-HTTPS to non-local hosts is always refused — protects Bearer token
        # from cleartext transmission.
        if scheme == "http" and not is_local:
            raise ValueError(
                f"MCP server '{hostname}' uses http:// — refusing to send "
                f"credentials over cleartext. Use https:// or a localhost/.internal name."
            )

        # Local/docker-internal: trusted, skip further checks
        if is_local:
            return

        # Resolve hostname to IP for CIDR matching
        try:
            ip_str = socket.gethostbyname(hostname)
            ip = ipaddress.ip_address(ip_str)
        except (socket.gaierror, ValueError):
            # DNS may not resolve in dev; allow but the network layer will fail safely
            return

        # ALWAYS block dangerous ranges regardless of allow-private flag
        for cidr in cls._BLOCKED_CIDRS:
            if ip in ipaddress.ip_network(cidr):
                raise ValueError(
                    f"MCP URL resolves to blocked IP range {cidr}: {ip}. "
                    f"This range is blocked to prevent cloud metadata / SSRF attacks."
                )

        # Other private IPs: allow only if explicitly enabled (production Docker networks)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            if os.environ.get("DOCKER_NETWORK_ALLOW_PRIVATE", "false").lower() != "true":
                raise ValueError(
                    f"MCP URL resolves to private IP {ip} and "
                    f"DOCKER_NETWORK_ALLOW_PRIVATE is not enabled."
                )

    async def initialize(self) -> dict:
        """MCP handshake: initialize → notifications/initialized."""
        self._validate_url(self.config.url)

        # Defense in depth: even if url validation was bypassed, refuse to send
        # Authorization over plain HTTP to non-localhost.
        import urllib.parse
        parsed = urllib.parse.urlparse(self.config.url)
        is_local = self._is_local_hostname(parsed.hostname or "")
        use_auth = bool(self.config.api_key) and (parsed.scheme == "https" or is_local)

        headers = {}
        if use_auth:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        elif self.config.api_key and not is_local:
            logger.error(
                f"MCP client {self.config.name}: api_key set but URL is http:// "
                f"— dropping Authorization header to avoid cleartext transmission."
            )

        self._http = httpx.AsyncClient(
            base_url=self.config.url,
            timeout=self.config.timeout,
            headers=headers,
        )

        result = await self._jsonrpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "ai-gateway", "version": "1.0.0"},
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
