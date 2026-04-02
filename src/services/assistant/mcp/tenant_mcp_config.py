"""
Tenant MCP Config — per-tenant MCP server access control.

ADR-002 Phase 1: Controls which MCP servers each tenant can access.
The existing MCPManager keeps a global pool of MCP clients;
this service adds a per-tenant filtering layer on top.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TenantMCPConfig:
    """Per-tenant MCP server access policy."""

    tenant_id: str
    allowed_servers: list[str] = field(default_factory=list)  # empty = allow all
    server_overrides: dict[str, Any] | None = None
    max_connections: int = 5


class TenantMCPConfigService:
    """Load per-tenant MCP configs and filter available MCP tools."""

    def __init__(self, database: Any, all_server_names: list[str] | None = None) -> None:
        self._database = database
        self._all_server_names = all_server_names or []
        self._cache: dict[str, TenantMCPConfig] = {}

    def set_all_server_names(self, names: list[str]) -> None:
        """Update the list of all known MCP server names (set after MCPManager init)."""
        self._all_server_names = names

    # ------------------------------------------------------------------
    # Config Loading
    # ------------------------------------------------------------------

    async def get_config(self, tenant_id: str) -> TenantMCPConfig:
        if tenant_id in self._cache:
            return self._cache[tenant_id]

        config = await self._load_from_db(tenant_id)
        self._cache[tenant_id] = config
        return config

    async def _load_from_db(self, tenant_id: str) -> TenantMCPConfig:
        if not self._database:
            return TenantMCPConfig(
                tenant_id=tenant_id,
                allowed_servers=list(self._all_server_names),
            )

        try:
            row = await self._database.fetchrow(
                "SELECT * FROM tenant_mcp_configs WHERE tenant_id = $1",
                tenant_id,
            )
        except Exception as e:
            logger.warning(f"Failed to load MCP config for {tenant_id}: {e}")
            return TenantMCPConfig(
                tenant_id=tenant_id,
                allowed_servers=list(self._all_server_names),
            )

        if not row:
            # Default: allow all configured servers
            return TenantMCPConfig(
                tenant_id=tenant_id,
                allowed_servers=list(self._all_server_names),
            )

        return TenantMCPConfig(
            tenant_id=tenant_id,
            allowed_servers=list(row["allowed_servers"] or []),
            server_overrides=row.get("server_overrides"),
            max_connections=row.get("max_connections", 5),
        )

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id:
            self._cache.pop(tenant_id, None)
        else:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_mcp_tools(
        self,
        tools: list,
        config: TenantMCPConfig,
    ) -> list:
        """Filter ToolDefinitions whose name starts with 'mcp_' by allowed servers.

        MCP tool names follow the pattern: mcp_{server_name}__{tool_name}
        """
        if not config.allowed_servers:
            # Empty list = deny all MCP tools
            return [t for t in tools if not t.name.startswith("mcp_")]

        allowed_prefixes = tuple(f"mcp_{s}__" for s in config.allowed_servers)

        result = []
        for tool in tools:
            if not tool.name.startswith("mcp_"):
                # Not an MCP tool — pass through
                result.append(tool)
            elif tool.name.startswith(allowed_prefixes):
                result.append(tool)
            # else: MCP tool from a non-allowed server — filtered out
        return result
