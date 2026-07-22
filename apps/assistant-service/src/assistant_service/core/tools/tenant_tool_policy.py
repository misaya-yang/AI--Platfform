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
    allowed_tools: set[str] = field(default_factory=set)  # whitelist (empty = allow all)
    blocked_tools: set[str] = field(default_factory=set)  # blacklist (precedence over whitelist)
    allowed_categories: set[str] = field(default_factory=set)  # e.g. {"retrieval", "generation"}
    max_calls_per_minute: int = 20


@dataclass(frozen=True)
class ResolvedAgentRuntimeResourcePolicy:
    """Request-scoped, non-expanding Agent resource decision."""

    tenant_id: str
    tool_names: frozenset[str]
    dataset_ids: frozenset[str]

    def allowed_tool_names(
        self,
        *,
        tenant_id: str,
        tool_names: frozenset[str],
    ) -> set[str]:
        if tenant_id != self.tenant_id:
            return set()
        return set(tool_names.intersection(self.tool_names))

    def allowed_dataset_ids(
        self,
        *,
        tenant_id: str,
        dataset_ids: frozenset[str],
    ) -> set[str]:
        if tenant_id != self.tenant_id:
            return set()
        return set(dataset_ids.intersection(self.dataset_ids))


class AgentRuntimeResourcePolicyService:
    """Strictly resolve current tenant tool and Dataset authority per Agent run."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        is_tenant_admin: bool,
        tool_names: frozenset[str],
        dataset_ids: frozenset[str],
    ) -> ResolvedAgentRuntimeResourcePolicy:
        """Resolve both policy dimensions atomically enough for request mapping.

        Unlike the legacy Assistant policy loader, this Agent-only path never
        substitutes an allow-all policy when PostgreSQL is unavailable.
        """

        pool = getattr(self._database, "_pool", None)
        if not getattr(self._database, "enabled", False) or pool is None:
            raise RuntimeError("Agent runtime resource policy is unavailable")

        from ai_gateway_core.persistence.repositories.agent_resource_resolver import (
            authorized_dataset_ids,
        )

        async with pool.acquire() as connection:
            policy_row = await connection.fetchrow(
                """
                SELECT allowed_tools, blocked_tools, allowed_categories
                FROM tenant_tool_policies
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            authorized_datasets = await authorized_dataset_ids(
                connection,
                tenant_id=tenant_id,
                user_id=user_id,
                dataset_ids=sorted(dataset_ids),
                is_tenant_admin=is_tenant_admin,
            )

        allowed = set(policy_row["allowed_tools"] or []) if policy_row else set()
        blocked = set(policy_row["blocked_tools"] or []) if policy_row else set()
        categories = set(policy_row["allowed_categories"] or []) if policy_row else set()
        # The signed Snapshot does not carry the legacy registry category for
        # every capability source. A configured category restriction therefore
        # cannot be proven here and must reduce the Agent tool set to empty.
        resolved_tools = (
            frozenset()
            if categories
            else frozenset(
                name
                for name in tool_names
                if name not in blocked and (not allowed or name in allowed)
            )
        )
        return ResolvedAgentRuntimeResourcePolicy(
            tenant_id=tenant_id,
            tool_names=resolved_tools,
            dataset_ids=frozenset(authorized_datasets.intersection(dataset_ids)),
        )


class TenantToolPolicyService:
    """Load and apply per-tenant tool policies from PostgreSQL."""

    def __init__(self, database: Any) -> None:
        self._database = database
        self._cache: dict[
            str, tuple[TenantToolPolicy, float]
        ] = {}  # tenant_id → (policy, expires_at)

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

    async def get_policy_fresh(self, tenant_id: str) -> TenantToolPolicy:
        """Re-read policy for a pre-execution revocation check."""

        policy = await self._load_from_db(tenant_id)
        self._cache[tenant_id] = (policy, time.monotonic() + _CACHE_TTL)
        return policy

    async def _load_from_db(self, tenant_id: str) -> TenantToolPolicy:
        if not self._database:
            return TenantToolPolicy(tenant_id=tenant_id)

        # ``DatabaseStorage.fetchrow`` intentionally returns ``None`` while its
        # pool is unavailable.  That is indistinguishable from a missing tenant
        # row unless readiness is checked first, and treating it as a missing
        # row would silently turn a storage outage into allow-all policy.
        if hasattr(self._database, "enabled") and not bool(self._database.enabled):
            raise RuntimeError("Tenant tool policy is unavailable")
        if hasattr(self._database, "_pool") and self._database._pool is None:
            raise RuntimeError("Tenant tool policy is unavailable")

        try:
            row = await self._database.fetchrow(
                "SELECT * FROM tenant_tool_policies WHERE tenant_id = $1",
                tenant_id,
            )
        except Exception as e:
            logger.warning(f"Failed to load tool policy for {tenant_id}: {e}")
            # A storage outage is not evidence of an allow-all policy. Let the
            # canonical catalog/invocation boundary convert this uncertainty
            # into a scoped deny decision.
            raise RuntimeError("Tenant tool policy is unavailable") from e

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
