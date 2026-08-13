"""
Connector Registry — tenant-conditional tool surface.

Unlike the global `ToolRegistry`, which stores tools that every request can
see, the `ConnectorRegistry` stores tools whose visibility depends on a
per-request predicate. The canonical example is Confluence: we don't want
every tenant's model to see `confluence_read` + `confluence_write` in its
tool list when only a handful of tenants have actually connected Confluence.

Each connector registers:
  - A unique ``connector_id`` (e.g. ``"confluence"``).
  - Its ``ToolDefinition`` objects + executors (registered with the global
    tool registry so execution paths find them when a call DOES arrive).
  - An async ``predicate(request) -> bool`` that answers "is this connector
    active for the caller?" — typically a tenant-scoped DB lookup.

At request-build time, the agent loop calls ``visible_tools(request)`` to
pick up just the tool definitions whose predicate returns True, and appends
them to the base tool list before handing it to the model.

Predicates are cached for 60s per (connector_id, tenant_id) to avoid DB
hammering during a multi-iteration agent turn — the same shape used by
`_TenantClientResolver` in `confluence_tool.py`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ai_gateway_core.logging import get_logger, record_internal_exception

from .tool_registry import ToolCallRequest, ToolDefinition

logger = get_logger(__name__)


ConnectorPredicate = Callable[[ToolCallRequest], Awaitable[bool]]


@dataclass
class _ConnectorEntry:
    """Internal record for a registered connector."""

    connector_id: str
    tool_defs: list[ToolDefinition]
    predicate: ConnectorPredicate
    cache_ttl_seconds: float = 60.0
    # cache key = tenant_id (or "" when the caller has no tenant_id).
    _cache: dict[str, tuple[float, bool]] = field(default_factory=dict)


def _tenant_of(request: ToolCallRequest) -> str:
    """Extract tenant_id from the request (user context first, metadata fallback)."""
    user = getattr(request, "user", None)
    tenant_id = ""
    if user is not None:
        tenant_id = str(getattr(user, "tenant_id", "") or "")
    if not tenant_id:
        tenant_id = str((getattr(request, "metadata", None) or {}).get("tenant_id") or "")
    return tenant_id


class ConnectorRegistry:
    """Holds tenant-conditional tools.

    Thread-safe for registration/lookup; predicate evaluation is async and
    intentionally NOT guarded by the lock (predicates may block on I/O).
    """

    def __init__(self) -> None:
        self._entries: dict[str, _ConnectorEntry] = {}
        self._lock = threading.RLock()

    def register(
        self,
        connector_id: str,
        tool_defs: list[ToolDefinition],
        predicate: ConnectorPredicate,
        *,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        """Register (or replace) a connector.

        Re-registering the same ``connector_id`` fully replaces the previous
        entry — including the cache. This matches the ToolRegistry semantics
        and lets tests swap predicates cleanly.
        """
        entry = _ConnectorEntry(
            connector_id=connector_id,
            tool_defs=list(tool_defs),
            predicate=predicate,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        with self._lock:
            if connector_id in self._entries:
                logger.info(f"Replacing connector registration: {connector_id}")
            self._entries[connector_id] = entry
        logger.info(
            f"Registered connector: {connector_id} "
            f"(tools={[t.name for t in tool_defs]}, ttl={cache_ttl_seconds}s)"
        )

    def unregister(self, connector_id: str) -> bool:
        with self._lock:
            if connector_id in self._entries:
                del self._entries[connector_id]
                return True
            return False

    def list_connectors(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())

    def get_tools(self, connector_id: str) -> list[ToolDefinition]:
        """Return the registered tools for a connector (without running the
        predicate). Useful for introspection / tests."""
        with self._lock:
            entry = self._entries.get(connector_id)
            return list(entry.tool_defs) if entry else []

    def connector_tool_names(self) -> set[str]:
        """All tool names claimed by ANY registered connector, ignoring
        predicates. The agent loop uses this to subtract connector-managed
        tools from the base `list_tools()` result before adding back only
        the ones a predicate allows. This keeps connector tools from
        leaking into the per-request model tool surface for tenants that
        haven't connected yet."""
        with self._lock:
            names: set[str] = set()
            for entry in self._entries.values():
                for td in entry.tool_defs:
                    names.add(td.name)
            return names

    async def visible_tools(self, request: ToolCallRequest) -> list[ToolDefinition]:
        """Return the union of tool definitions for every connector whose
        predicate returns True for this request."""
        with self._lock:
            entries = list(self._entries.values())

        results: list[ToolDefinition] = []
        for entry in entries:
            try:
                active = await self._evaluate(entry, request)
            except Exception as exc:
                # Predicate blew up — log but don't break the whole request.
                # Safer to hide the connector than to crash the chat turn.
                record_internal_exception(
                    __name__, "assistant.core.tools.connector_registry.internal_failure", exc
                )
                continue
            if active:
                results.extend(entry.tool_defs)
        return results

    async def is_active(self, connector_id: str, request: ToolCallRequest) -> bool:
        """Check a single connector. Convenience for dispatch / diagnostics."""
        with self._lock:
            entry = self._entries.get(connector_id)
        if entry is None:
            return False
        try:
            return await self._evaluate(entry, request)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.tools.connector_registry.internal_failure", exc
            )
            return False

    def invalidate(self, connector_id: str | None = None, tenant_id: str | None = None) -> None:
        """Clear cached predicate results.

        - ``connector_id=None``: clears all connectors.
        - ``tenant_id=None``: clears all tenants for the chosen connector(s).
        """
        with self._lock:
            targets = (
                [self._entries[connector_id]]
                if connector_id and connector_id in self._entries
                else list(self._entries.values())
            )
            for entry in targets:
                if tenant_id is None:
                    entry._cache.clear()
                else:
                    entry._cache.pop(tenant_id, None)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _evaluate(self, entry: _ConnectorEntry, request: ToolCallRequest) -> bool:
        tenant_id = _tenant_of(request)
        cached = entry._cache.get(tenant_id)
        if cached is not None:
            ts, value = cached
            if (time.time() - ts) <= entry.cache_ttl_seconds:
                return value
            # expired — fall through to re-evaluate
        result = await entry.predicate(request)
        entry._cache[tenant_id] = (time.time(), bool(result))
        return bool(result)


# Global singleton — mirrors get_tool_registry()'s shape.
_connector_registry: ConnectorRegistry | None = None
_singleton_lock = threading.Lock()


def get_connector_registry() -> ConnectorRegistry:
    """Get the global connector registry."""
    global _connector_registry
    if _connector_registry is None:
        with _singleton_lock:
            if _connector_registry is None:
                _connector_registry = ConnectorRegistry()
    return _connector_registry


def reset_connector_registry_for_tests() -> None:
    """Reset the global singleton — tests only. Production code never calls this."""
    global _connector_registry
    with _singleton_lock:
        _connector_registry = None


__all__ = [
    "ConnectorPredicate",
    "ConnectorRegistry",
    "get_connector_registry",
    "reset_connector_registry_for_tests",
]
