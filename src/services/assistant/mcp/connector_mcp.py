"""
Connector MCP Service — dynamically spawns MCP servers when users connect services.

When a user connects Confluence (or other services), this spawns the appropriate
MCP server process (e.g., mcp-atlassian) with the user's credentials as env vars.
The MCPManager then discovers tools and registers them in the ToolRegistry.

Supported connectors:
  - Confluence: spawns `uvx mcp-atlassian` with CONFLUENCE_URL/USERNAME/API_TOKEN
  - (Future: GitHub, Slack, etc.)

Flow:
  1. User connects via ConnectorsPanel → credentials saved to DB
  2. Backend calls ConnectorMCPService.start_connector()
  3. Spawns MCP server process with credentials as env vars
  4. MCPClient connects via stdio, discovers tools
  5. Tools registered as mcp_confluence__search, mcp_confluence__get_page, etc.
  6. AI can now call these tools during chat
  7. On disconnect, process is stopped and tools deregistered
"""

from __future__ import annotations

import asyncio
from typing import Any

from ....core.observability.logging import get_logger
from .client import MCPClient, MCPServerConfig, MCPTool

logger = get_logger(__name__)


class ConnectorMCPService:
    """Manages MCP server processes for connected third-party services."""

    def __init__(self) -> None:
        # tenant_id:provider -> MCPClient
        self._clients: dict[str, MCPClient] = {}
        # tenant_id:provider -> list of tool defs
        self._tools: dict[str, list[MCPTool]] = {}

    async def start_confluence(
        self,
        tenant_id: str,
        domain: str,
        email: str,
        api_token: str,
    ) -> list[MCPTool]:
        """Start mcp-atlassian server for a Confluence connection.

        Args:
            tenant_id: Tenant identifier
            domain: Confluence domain (e.g., mycompany.atlassian.net)
            email: User email for auth
            api_token: Confluence API token

        Returns:
            List of discovered MCP tools
        """
        key = f"{tenant_id}:confluence"

        # Stop existing if running
        if key in self._clients:
            await self.stop_connector(tenant_id, "confluence")

        confluence_url = f"https://{domain}/wiki"

        config = MCPServerConfig(
            name=f"confluence_{tenant_id}",
            # Use uvx to run mcp-atlassian (installed in server environment)
            # Falls back to python -m mcp_atlassian if uvx not available
            command="uvx",
            args=["mcp-atlassian"],
            env={
                "CONFLUENCE_URL": confluence_url,
                "CONFLUENCE_USERNAME": email,
                "CONFLUENCE_API_TOKEN": api_token,
                # Disable Jira (only Confluence)
                "JIRA_URL": "",
            },
        )

        client = MCPClient(config)
        try:
            tools = await asyncio.wait_for(client.connect(), timeout=30)
            self._clients[key] = client
            self._tools[key] = tools

            logger.info(
                f"Confluence MCP started for tenant {tenant_id}: "
                f"{len(tools)} tools discovered from {confluence_url}"
            )
            return tools

        except asyncio.TimeoutError:
            logger.error(f"Confluence MCP connection timed out for tenant {tenant_id}")
            await client.close()
            raise RuntimeError("Confluence MCP server timed out. Is mcp-atlassian installed?")

        except FileNotFoundError:
            logger.error("mcp-atlassian not found. Install with: pip install mcp-atlassian")
            raise RuntimeError(
                "mcp-atlassian not installed on server. "
                "Install with: pip install mcp-atlassian"
            )

        except Exception as exc:
            logger.error(f"Failed to start Confluence MCP: {exc}")
            await client.close()
            raise

    async def stop_connector(self, tenant_id: str, provider: str) -> None:
        """Stop an MCP server for a connector."""
        key = f"{tenant_id}:{provider}"
        client = self._clients.pop(key, None)
        if client:
            await client.close()
            self._tools.pop(key, None)
            logger.info(f"Stopped {provider} MCP for tenant {tenant_id}")

    async def call_tool(
        self,
        tenant_id: str,
        provider: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool on a connected MCP server."""
        key = f"{tenant_id}:{provider}"
        client = self._clients.get(key)
        if not client:
            raise RuntimeError(f"No {provider} MCP connection for tenant {tenant_id}")
        return await client.call_tool(tool_name, arguments)

    def get_tools(self, tenant_id: str, provider: str) -> list[MCPTool]:
        """Get tools for a connected provider."""
        key = f"{tenant_id}:{provider}"
        return self._tools.get(key, [])

    def is_connected(self, tenant_id: str, provider: str) -> bool:
        """Check if a provider MCP is running."""
        return f"{tenant_id}:{provider}" in self._clients

    @property
    def active_connections(self) -> list[str]:
        """List all active tenant:provider connections."""
        return list(self._clients.keys())

    async def stop_all(self) -> None:
        """Stop all MCP servers. Called on app shutdown."""
        for key, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as exc:
                logger.warning(f"Error stopping MCP {key}: {exc}")
        self._clients.clear()
        self._tools.clear()


# Singleton instance
_connector_mcp_service: ConnectorMCPService | None = None


def get_connector_mcp_service() -> ConnectorMCPService:
    """Get or create the singleton ConnectorMCPService."""
    global _connector_mcp_service
    if _connector_mcp_service is None:
        _connector_mcp_service = ConnectorMCPService()
    return _connector_mcp_service
