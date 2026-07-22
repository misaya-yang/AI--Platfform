"""
Tenant MCP Config — per-tenant MCP server access control.

ADR-002 Phase 1: Controls which MCP servers each tenant can access.
The existing MCPManager keeps a global pool of MCP clients;
this service adds a per-tenant filtering layer on top.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)

_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX_SIZE = 500


@dataclass
class TenantMCPConfig:
    """Per-tenant MCP server access policy."""

    tenant_id: str
    allowed_servers: set[str] = field(default_factory=set)  # empty = deny all MCP
    server_overrides: dict[str, Any] | None = None
    max_connections: int = 5
    policy_source: str = "configured"


class TenantMCPConfigService:
    """Load per-tenant MCP configs and filter available MCP tools."""

    def __init__(self, database: Any, all_server_names: list[str] | None = None) -> None:
        self._database = database
        self._all_server_names = set(all_server_names or [])
        self._cache: dict[
            str, tuple[TenantMCPConfig, float]
        ] = {}  # tenant_id → (config, expires_at)

    def set_all_server_names(self, names: list[str]) -> None:
        """Update the list of all known MCP server names (set after MCPManager init)."""
        self._all_server_names = set(names)

    # ------------------------------------------------------------------
    # Config Loading
    # ------------------------------------------------------------------

    async def get_config(self, tenant_id: str) -> TenantMCPConfig:
        entry = self._cache.get(tenant_id)
        if entry and entry[1] > time.monotonic():
            return entry[0]

        config = await self._load_from_db(tenant_id)
        if len(self._cache) >= _CACHE_MAX_SIZE:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[tenant_id] = (config, time.monotonic() + _CACHE_TTL)
        return config

    async def get_config_fresh(self, tenant_id: str) -> TenantMCPConfig:
        """Re-read policy for a pre-execution revocation check."""

        config = await self._load_from_db(tenant_id)
        self._cache[tenant_id] = (config, time.monotonic() + _CACHE_TTL)
        return config

    async def _load_from_db(self, tenant_id: str) -> TenantMCPConfig:
        if not self._database:
            return TenantMCPConfig(
                tenant_id=tenant_id,
                allowed_servers=set(),
                policy_source="default_deny_no_database",
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
                allowed_servers=set(),
                policy_source="default_deny_config_error",
            )

        if not row:
            return TenantMCPConfig(
                tenant_id=tenant_id,
                allowed_servers=set(),
                policy_source="default_deny_missing_config",
            )

        return TenantMCPConfig(
            tenant_id=tenant_id,
            allowed_servers=set(row["allowed_servers"] or []),
            server_overrides=row.get("server_overrides"),
            max_connections=row.get("max_connections", 5),
            policy_source="configured",
        )

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id:
            self._cache.pop(tenant_id, None)
        else:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @staticmethod
    def filter_mcp_tools(
        tools: list,
        config: TenantMCPConfig,
    ) -> list:
        """Filter ToolDefinitions whose name starts with 'mcp_' by allowed servers.

        MCP tool names follow the pattern: mcp_{server_name}__{tool_name}
        """
        if not config.allowed_servers:
            # Empty set = deny all MCP tools
            return [t for t in tools if not t.name.startswith("mcp_")]

        allowed_prefixes = tuple(f"mcp_{s}__" for s in config.allowed_servers)

        result = []
        for tool in tools:
            if not tool.name.startswith("mcp_") or tool.name.startswith(allowed_prefixes):
                result.append(tool)
        return result
