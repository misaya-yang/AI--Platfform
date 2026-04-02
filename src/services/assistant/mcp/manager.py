"""
MCP Manager — multi-server orchestration and tool registration.

Connects to all configured MCP servers, discovers their tools, and registers
them into the agent's ToolRegistry with prefix `mcp_{server}:{tool}`.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import MCPClient, MCPServerConfig, MCPTool
from ..tools.tool_registry import (
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
    get_tool_registry,
)

logger = logging.getLogger(__name__)


class MCPManager:
    """Manage multiple MCP server connections and tool registration."""

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self._configs = configs or []
        self._clients: dict[str, MCPClient] = {}

    async def initialize_all(self) -> dict[str, int]:
        """Connect to all configured MCP servers and discover tools.

        Returns: {server_name: tool_count} (-1 if failed)
        """
        results: dict[str, int] = {}
        tool_registry = get_tool_registry()

        # Parallel initialization of all servers
        import asyncio

        async def _init_one(config: MCPServerConfig) -> tuple[str, int]:
            if not config.enabled:
                return config.name, 0
            try:
                client = MCPClient(config)
                await client.initialize()
                tools = await client.list_tools()
                self._clients[config.name] = client
                for mcp_tool in tools:
                    self._register_mcp_tool(mcp_tool, client, tool_registry)
                logger.info(f"MCP '{config.name}': {len(tools)} tools registered")
                return config.name, len(tools)
            except Exception as e:
                logger.warning(f"MCP '{config.name}' failed: {e}")
                return config.name, -1

        init_results = await asyncio.gather(
            *[_init_one(c) for c in self._configs],
            return_exceptions=True,
        )
        for r in init_results:
            if isinstance(r, Exception):
                continue
            results[r[0]] = r[1]

        return results

    def _register_mcp_tool(
        self, mcp_tool: MCPTool, client: MCPClient, tool_registry: Any,
    ) -> None:
        """Register an MCP tool as a callable tool in ToolRegistry."""
        # Sanitize tool name for ToolRegistry (no colons allowed in some LLM APIs)
        registry_name = f"mcp_{mcp_tool.server_name}__{mcp_tool.name}"

        # Convert JSON Schema to ToolParameters
        params = self._schema_to_params(mcp_tool.input_schema)

        definition = ToolDefinition(
            name=registry_name,
            description=f"[{mcp_tool.server_name}] {mcp_tool.description}",
            parameters=params,
            category=ToolCategory.MCP,
            risk_level=ToolRiskLevel.MEDIUM,
            when_to_use=mcp_tool.description,
            timeout_seconds=int(client.config.timeout),
            is_async=True,
        )

        # Create executor closure
        async def executor(request: Any) -> Any:
            from ..tools.tool_registry import ToolCallResult
            args = getattr(request, "arguments", None) or getattr(request, "tool_args", {}) or {}
            result = await client.call_tool(mcp_tool.name, args)
            text_parts = []
            for c in result.content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text_parts.append(str(c.get("text", "")))
            return ToolCallResult(
                call_id=getattr(request, "call_id", ""),
                tool_name=registry_name,
                success=not result.is_error,
                result="\n".join(text_parts) if text_parts else str(result.content),
                metadata={"mcp_server": mcp_tool.server_name, "mcp_tool": mcp_tool.name},
            )

        tool_registry.register(definition, executor)

    async def refresh_tools(self, server_name: str | None = None) -> dict[str, int]:
        """Re-discover tools from MCP servers. Deregisters stale tools."""
        tool_registry = get_tool_registry()
        targets = [server_name] if server_name else list(self._clients.keys())
        results: dict[str, int] = {}
        for name in targets:
            client = self._clients.get(name)
            if not client:
                results[name] = -1
                continue
            try:
                # Deregister old tools for this server before re-registering
                prefix = f"mcp_{name}__"
                for existing in tool_registry.list_tools():
                    if existing.name.startswith(prefix):
                        tool_registry.unregister(existing.name)

                tools = await client.list_tools()
                for t in tools:
                    self._register_mcp_tool(t, client, tool_registry)
                results[name] = len(tools)
            except Exception as e:
                logger.warning(f"MCP refresh '{name}' failed: {e}")
                results[name] = -1
        return results

    async def shutdown(self) -> None:
        """Close all MCP connections."""
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()

    @property
    def server_names(self) -> list[str]:
        """Public accessor for configured server names."""
        return [c.name for c in self._configs]

    def get_servers_status(self) -> list[dict]:
        """Return status of all configured MCP servers."""
        status = []
        for config in self._configs:
            client = self._clients.get(config.name)
            status.append({
                "name": config.name,
                "url": config.url,
                "description": config.description,
                "enabled": config.enabled,
                "connected": client is not None and client.is_initialized,
                "tool_count": len(client.tools) if client else 0,
            })
        return status

    def _schema_to_params(self, schema: dict) -> list[ToolParameter]:
        """Convert JSON Schema to ToolParameter list."""
        params = []
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        for name, prop in properties.items():
            params.append(ToolParameter(
                name=name,
                type=prop.get("type", "string"),
                description=prop.get("description", ""),
                required=name in required,
            ))
        return params
