"""
Tenant Tool Policy — per-tenant tool access control.

ADR-002 Phase 1: Filters available tools by tenant whitelist/blacklist
and enforces per-tenant rate limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

if TYPE_CHECKING:
    from .tool_registry import ToolDefinition

logger = get_logger(__name__)

# Cache settings
_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX_SIZE = 500


@dataclass
class TenantToolPolicy:
    """Per-tenant tool access policy."""

    tenant_id: str
    allowed_tools: set[str] = field(default_factory=set)       # whitelist (empty = allow all)
    blocked_tools: set[str] = field(default_factory=set)       # blacklist (precedence over whitelist)
    allowed_categories: set[str] = field(default_factory=set)  # e.g. {"retrieval", "generation"}
    max_calls_per_minute: int = 20


class TenantToolPolicyService:
    """Load and apply per-tenant tool policies from PostgreSQL."""

    def __init__(self, database: Any) -> None:
        self._database = database
        self._cache: dict[str, tuple[TenantToolPolicy, float]] = {}  # tenant_id → (policy, expires_at)

    # ------------------------------------------------------------------
    # Policy Loading
    # ------------------------------------------------------------------

    async def get_policy(self, tenant_id: str) -> TenantToolPolicy:
        """Get policy for a tenant, from cache (with TTL) or DB."""
        entry = self._cache.get(tenant_id)
        if entry and entry[1] > time.monotonic():
            return entry[0]

        policy = await self._load_from_db(tenant_id)
        # Evict oldest entries if cache is full
        if len(self._cache) >= _CACHE_MAX_SIZE:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[tenant_id] = (policy, time.monotonic() + _CACHE_TTL)
        return policy

    async def _load_from_db(self, tenant_id: str) -> TenantToolPolicy:
        if not self._database:
            return TenantToolPolicy(tenant_id=tenant_id)

        try:
            row = await self._database.fetchrow(
                "SELECT * FROM tenant_tool_policies WHERE tenant_id = $1",
                tenant_id,
            )
        except Exception as e:
            logger.warning(f"Failed to load tool policy for {tenant_id}: {e}")
            return TenantToolPolicy(tenant_id=tenant_id)

        if not row:
            return TenantToolPolicy(tenant_id=tenant_id)

        return TenantToolPolicy(
            tenant_id=tenant_id,
            allowed_tools=set(row["allowed_tools"] or []),
            blocked_tools=set(row["blocked_tools"] or []),
            allowed_categories=set(row["allowed_categories"] or []),
            max_calls_per_minute=row.get("max_calls_per_minute", 20),
        )

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Clear cached policy. Pass None to clear all."""
        if tenant_id:
            self._cache.pop(tenant_id, None)
        else:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_tools(
        self,
        tools: list[ToolDefinition],
        policy: TenantToolPolicy,
    ) -> list[ToolDefinition]:
        """Filter tools based on tenant policy (blacklist > whitelist > categories)."""
        result: list[ToolDefinition] = []
        for tool in tools:
            # Blacklist takes precedence
            if tool.name in policy.blocked_tools:
                continue
            # Whitelist (empty = allow all)
            if policy.allowed_tools and tool.name not in policy.allowed_tools:
                continue
            # Category filter (empty = allow all)
            if policy.allowed_categories and tool.category.value not in policy.allowed_categories:
                continue
            result.append(tool)
        return result
