"""
Tool Invoker - Unified Tool Calling Entry Point

This module provides an abstract interface for tool execution, enabling:
- Decoupling of tool orchestration from execution implementation
- Plugin architecture for different execution backends
- Centralized logging, metrics, and rate limiting
- Easier testing with mock invokers

Design Philosophy:
- Abstract ToolInvoker interface defines the contract
- RegistryToolInvoker provides concrete implementation via ToolRegistry
- Future implementations could route to remote services, sandboxes, etc.

Usage:
    ```python
    invoker = create_tool_invoker()

    context = ToolInvocationContext(
        session_id="session_123",
        user_id="user_1",
        tenant_id="tenant_1",
        request_id=str(uuid.uuid4()),
    )

    result = await invoker.invoke(
        tool_name="kb_search",
        arguments={"query": "产品规格"},
        context=context,
    )
    ```

References:
- Enterprise AI Agent patterns
- Strategy pattern for pluggable execution backends
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json as _json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import redact_trace_text

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike

    from .tools.tool_registry import ToolCallRequest, ToolCallResult, ToolDefinition, ToolRegistry

logger = get_logger(__name__)

_MAX_PUBLIC_ERROR_CHARS = 200


def _safe_public_error(value: Any) -> str:
    """Return a bounded, shared-redaction error while preserving safe short messages."""

    try:
        text = str(value) if isinstance(value, BaseException) else value
        return redact_trace_text(text, limit=_MAX_PUBLIC_ERROR_CHARS)
    except Exception:
        return "Tool execution failed"


def _tool_log_label(tool_name: Any) -> str:
    """Return a stable non-reversible label for untrusted tool names."""

    try:
        digest = hashlib.sha256(str(tool_name).encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return "tool_sha256=unavailable"
    return f"tool_sha256={digest[:16]}"


def _log_audit_task_completion(task: asyncio.Task[Any], tool_label: str) -> None:
    """Retrieve an async audit failure without exposing its exception payload."""

    if task.cancelled():
        return
    try:
        error = task.exception()
    except Exception as exc:
        logger.debug(
            "Tool audit callback inspection failed (tool_label=%s, exception_type=%s)",
            tool_label,
            type(exc).__name__,
        )
        return
    if error is not None:
        logger.debug(
            "Async tool audit failed (tool_label=%s, exception_type=%s)",
            tool_label,
            type(error).__name__,
        )


# =============================================================================
# Data Classes
# =============================================================================


class _CopiedBindingMap(Mapping[str, dict[str, Any]]):
    """Read-only binding map whose nested values are never shared with callers."""

    def __init__(self, values: Mapping[str, dict[str, Any]]) -> None:
        self._values = copy.deepcopy(dict(values))

    def __getitem__(self, key: str) -> dict[str, Any]:
        return copy.deepcopy(self._values[key])

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True)
class CapabilityAllowlist:
    """Hard upper bound for tool capabilities available to one Agent run.

    The absence of this object preserves the legacy built-in Assistant tool
    surface. An explicit object, including one with no names, may only reduce
    that surface. Tenant, permission, health, and connector checks can further
    reduce it; they can never add a name that is absent here.
    """

    tool_names: frozenset[str] = field(default_factory=frozenset)
    bindings: Mapping[str, dict[str, Any]] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.tool_names, str):
            raise TypeError("tool_names must be a collection of complete tool names")
        object.__setattr__(
            self,
            "tool_names",
            frozenset(str(name) for name in self.tool_names),
        )
        object.__setattr__(
            self,
            "bindings",
            _CopiedBindingMap(
                {
                    str(name): copy.deepcopy(binding)
                    for name, binding in dict(self.bindings or {}).items()
                    if str(name) in self.tool_names and isinstance(binding, dict)
                }
            ),
        )

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.tool_names

    def binding(self, tool_name: str) -> dict[str, Any] | None:
        value = self.bindings.get(tool_name)
        return copy.deepcopy(value) if value is not None else None

    def filter_definitions(
        self,
        tools: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        return [tool for tool in tools if self.allows(tool.name)]


@dataclass(frozen=True)
class ToolPolicySnapshot:
    """Immutable upper bound shared by catalog and invocation for one run.

    A live execution check may only remove capabilities from this snapshot. It
    can never add a tool that was absent when the model-facing catalog was
    compiled. The identity fields prevent a snapshot from being reused across
    tenants, users, sessions, or runs.
    """

    tenant_id: str
    user_id: str
    session_id: str
    run_scope: str
    identity_resolved: bool = True
    tool_policy_enabled: bool = False
    tool_policy_resolved: bool = True
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    blocked_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_categories: frozenset[str] = field(default_factory=frozenset)
    mcp_policy_enabled: bool = False
    mcp_policy_resolved: bool = True
    allowed_mcp_servers: frozenset[str] = field(default_factory=frozenset)
    mcp_policy_source: str = "not_configured"
    snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "allowed_tools",
            "blocked_tools",
            "allowed_categories",
            "allowed_mcp_servers",
        ):
            value = getattr(self, name)
            if isinstance(value, str):
                raise TypeError(f"{name} must be a collection")
            object.__setattr__(self, name, frozenset(str(item) for item in value))
        payload = {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "run_scope": self.run_scope,
            "identity_resolved": self.identity_resolved,
            "tool_policy_enabled": self.tool_policy_enabled,
            "tool_policy_resolved": self.tool_policy_resolved,
            "allowed_tools": sorted(self.allowed_tools),
            "blocked_tools": sorted(self.blocked_tools),
            "allowed_categories": sorted(self.allowed_categories),
            "mcp_policy_enabled": self.mcp_policy_enabled,
            "mcp_policy_resolved": self.mcp_policy_resolved,
            "allowed_mcp_servers": sorted(self.allowed_mcp_servers),
            "mcp_policy_source": self.mcp_policy_source,
        }
        digest = hashlib.sha256(
            _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        object.__setattr__(self, "snapshot_id", f"tps_{digest}")

    @classmethod
    def denied_for(cls, context: ToolInvocationContext) -> ToolPolicySnapshot:
        """Create a scope-bound deny-all snapshot for an invalid identity."""

        return cls(
            tenant_id=str(context.tenant_id or ""),
            user_id=str(context.user_id or ""),
            session_id=str(context.session_id or ""),
            run_scope=str(context.run_id or context.request_id or ""),
            identity_resolved=False,
            tool_policy_resolved=False,
            mcp_policy_resolved=False,
            mcp_policy_source="identity_unresolved",
        )

    def matches(self, context: ToolInvocationContext) -> bool:
        return (
            self.tenant_id == str(context.tenant_id or "")
            and self.user_id == str(context.user_id or "")
            and self.session_id == str(context.session_id or "")
            and self.run_scope == str(context.run_id or context.request_id or "")
        )

    def allows(
        self,
        tool_name: str,
        *,
        category: str | None,
        binding_type: str = "",
    ) -> bool:
        if not self.identity_resolved or not self.tool_policy_resolved:
            return False
        if self.tool_policy_enabled:
            if tool_name in self.blocked_tools:
                return False
            if self.allowed_tools and tool_name not in self.allowed_tools:
                return False
            if self.allowed_categories and (
                not category or category not in self.allowed_categories
            ):
                return False
        if tool_name.startswith("mcp_") and binding_type != "mcp":
            if not self.mcp_policy_resolved:
                return False
            if self.mcp_policy_enabled:
                prefixes = tuple(f"mcp_{name}__" for name in self.allowed_mcp_servers)
                if not prefixes or not tool_name.startswith(prefixes):
                    return False
        return True


@dataclass(frozen=True)
class ToolExecutionPolicy:
    """Trusted retry and side-effect facts for one tool operation."""

    operation_kind: str
    operation_id: str
    operation_fingerprint: str = ""
    external_service: bool = False
    idempotency_key: str | None = None
    idempotency_supported: bool = False
    read_back_available: bool = False
    compensation_available: bool = False
    max_attempts: int = 1

    @property
    def side_effecting(self) -> bool:
        return self.operation_kind != "read"

    @property
    def replay_safe(self) -> bool:
        return self.operation_kind == "read" or bool(
            self.idempotency_supported and self.idempotency_key
        )

    @property
    def may_have_external_side_effect(self) -> bool:
        # ``unknown`` is not evidence of read-only behavior. Treat it like a
        # potentially irreversible write until repository-owned metadata proves
        # otherwise, regardless of whether ``external_service`` was declared.
        return self.side_effecting

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_kind": self.operation_kind,
            "operation_id": self.operation_id,
            "operation_fingerprint": self.operation_fingerprint,
            "external_service": self.external_service,
            "idempotency_key_present": bool(self.idempotency_key),
            "idempotency_supported": self.idempotency_supported,
            "read_back_available": self.read_back_available,
            "compensation_available": self.compensation_available,
            "max_attempts": self.max_attempts,
        }


@dataclass
class ToolInvocationContext:
    """
    Context for tool invocation with session and user info.

    This context travels with every tool invocation, providing:
    - Session isolation (session_id)
    - Multi-tenancy support (tenant_id)
    - User identity (user_id)
    - Request tracing (request_id, run_id)
    - Execution constraints (timeout, retries)

    Attributes:
        session_id: Unique session identifier for isolation
        user_id: User making the request
        tenant_id: Tenant for multi-tenancy isolation
        request_id: Unique identifier for this request (for tracing)
        run_id: Optional run identifier for agent execution
        timeout_ms: Maximum execution time in milliseconds
        max_retries: Maximum retry attempts on transient failures
        parent_task_id: For nested invocations, the parent task
        metadata: Additional context for logging/analytics
    """

    session_id: str
    user_id: str
    tenant_id: str
    request_id: str
    run_id: str | None = None

    # Execution constraints
    timeout_ms: int = 30000  # 30 seconds default
    max_retries: int = 2

    # Parent task context (for nested invocations)
    parent_task_id: str | None = None

    # Isolation and policy metadata
    scope_id: str | None = None
    policy_profile: str = "safe"
    os_agent_enabled: bool = False

    # Knowledge Base context - auto-injected into KB search tools
    kb_dataset_ids: list[str] = field(default_factory=list)

    # User context - required for tools that need user permissions (e.g., KB search)
    user: UserContextLike | None = None

    # Agent capability boundary. ``None`` preserves legacy Assistant behavior;
    # an explicit allowlist (including empty) can only reduce visible/invokable
    # tools and is enforced again immediately before invocation.
    capability_allowlist: CapabilityAllowlist | None = None

    # Immutable catalog-time authorization ceiling. It is populated by the
    # async catalog/invocation boundary and intentionally omitted from
    # ``to_dict`` except for its opaque digest.
    policy_snapshot: ToolPolicySnapshot | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    # Run-local side-effect fence. These opaque fingerprints are shared by
    # parent/child invocation contexts but are not serialized into logs.
    uncertain_operation_fingerprints: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )
    inflight_operation_fingerprints: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )

    # Per-run tools (for example exact tenant Skill versions) live outside the
    # process-global registry.  The field is deliberately omitted from
    # ``to_dict`` so definitions, executors, and instruction content cannot
    # enter traces/checkpoints through context serialization.
    runtime_tool_registry: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    # Metadata for logging and analytics
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "timeout_ms": self.timeout_ms,
            "max_retries": self.max_retries,
            "parent_task_id": self.parent_task_id,
            "scope_id": self.scope_id,
            "policy_profile": self.policy_profile,
            "os_agent_enabled": self.os_agent_enabled,
            "kb_dataset_ids": self.kb_dataset_ids,
            "capability_allowlist": (
                None
                if self.capability_allowlist is None
                else sorted(self.capability_allowlist.tool_names)
            ),
            "policy_snapshot_id": (
                self.policy_snapshot.snapshot_id if self.policy_snapshot is not None else None
            ),
            "metadata": self.metadata,
        }


@dataclass
class BatchInvocationResult:
    """
    Result of a batch tool invocation.

    Contains all results plus aggregate statistics.
    """

    results: list[ToolCallResult]
    total_duration_ms: float
    successful_count: int
    failed_count: int

    @property
    def all_successful(self) -> bool:
        """Check if all invocations succeeded."""
        return self.failed_count == 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "results": [r.to_dict() for r in self.results],
            "total_duration_ms": self.total_duration_ms,
            "successful_count": self.successful_count,
            "failed_count": self.failed_count,
            "all_successful": self.all_successful,
        }


# =============================================================================
# Abstract Interface
# =============================================================================


class ToolInvoker(ABC):
    """
    Abstract base class for unified tool execution.

    This interface defines the contract for tool invocation, enabling:
    - Multiple implementation strategies (registry, remote, sandbox)
    - Consistent error handling and logging
    - Metrics collection and rate limiting
    - Testing with mock implementations

    Implementations must provide:
    - invoke(): Single tool execution
    - invoke_batch(): Multiple tool execution with optional parallelism
    - get_available_tools(): List available tools for context
    """

    @abstractmethod
    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolCallResult:
        """
        Invoke a tool with the given arguments and context.

        Args:
            tool_name: Name of the tool to invoke
            arguments: Tool arguments as key-value pairs
            context: Invocation context with session info
            cancel_event: Optional event to signal cancellation

        Returns:
            ToolCallResult with execution outcome

        Raises:
            No exceptions - errors returned in ToolCallResult
        """
        pass

    @abstractmethod
    async def invoke_batch(
        self,
        requests: list[dict[str, Any]],
        context: ToolInvocationContext,
        parallel: bool = True,
        max_concurrency: int = 5,
    ) -> BatchInvocationResult:
        """
        Invoke multiple tools, optionally in parallel.

        Args:
            requests: List of dicts with 'tool_name' and 'arguments' keys
            context: Shared invocation context
            parallel: Whether to execute in parallel (True) or sequential (False)
            max_concurrency: Maximum concurrent executions when parallel=True

        Returns:
            BatchInvocationResult with all results in request order
        """
        pass

    @abstractmethod
    def get_available_tools(
        self,
        context: ToolInvocationContext,
    ) -> list[str]:
        """
        Get list of tool names available for this context.

        Args:
            context: Invocation context (may filter by permissions)

        Returns:
            List of available tool names
        """
        pass

    @abstractmethod
    def get_tool_definitions(
        self,
        context: ToolInvocationContext,
        tool_names: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """
        Get tool definitions for schema generation.

        Args:
            context: Invocation context
            tool_names: Optional filter for specific tools

        Returns:
            List of ToolDefinition objects
        """
        pass

    async def get_tool_definitions_filtered(
        self,
        context: ToolInvocationContext,
        tool_names: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """Get tool definitions with optional per-tenant filtering.

        Default implementation delegates to synchronous `get_tool_definitions()`.
        Subclasses may override to add tenant policy / MCP filtering.
        """
        return self.get_tool_definitions(context, tool_names)

    async def filter_tool_definitions_authorized(
        self,
        context: ToolInvocationContext,
        tools: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        """Recheck externally merged definitions at the invocation boundary."""

        if context.capability_allowlist is None:
            return list(tools)
        return context.capability_allowlist.filter_definitions(tools)


# =============================================================================
# Concrete Implementation
# =============================================================================


class RegistryToolInvoker(ToolInvoker):
    """
    Concrete implementation that routes to ToolRegistry.

    Provides:
    - Rate limiting per tenant/user (optional)
    - Execution metrics collection (optional)
    - Timeout handling with cancellation
    - Error normalization
    - Retry logic for transient failures

    Usage:
        ```python
        invoker = RegistryToolInvoker(
            tool_registry=get_tool_registry(),
            rate_limiter=RateLimiter(max_per_minute=100),
        )

        result = await invoker.invoke(
            tool_name="generate_document",
            arguments={"title": "报告"},
            context=context,
        )
        ```
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        rate_limiter: Callable[[str, str], bool] | None = None,
        metrics_collector: Callable[[str, float, bool], None] | None = None,
        tenant_tool_policy: Any | None = None,
        tenant_mcp_config: Any | None = None,
        mcp_runtime: Any | None = None,
        tool_audit: Any | None = None,
    ):
        """
        Initialize the RegistryToolInvoker.

        Args:
            tool_registry: The ToolRegistry to delegate execution to
            rate_limiter: Optional callable(tenant_id, tool_name) -> bool
                          Returns True if request should be rate limited
            metrics_collector: Optional callable(tool_name, duration_ms, success)
                               Called after each invocation for metrics
            tenant_tool_policy: TenantToolPolicyService for per-tenant tool filtering
            tenant_mcp_config: TenantMCPConfigService for per-tenant MCP filtering
            tool_audit: ToolAuditService for audit logging
        """
        self.tool_registry = tool_registry
        self.rate_limiter = rate_limiter
        self.metrics_collector = metrics_collector
        self.tenant_tool_policy = tenant_tool_policy
        self.tenant_mcp_config = tenant_mcp_config
        self.mcp_runtime = mcp_runtime
        self.tool_audit = tool_audit

        # ADR-003 Phase 3: Principal-and-scope isolated tool result cache.
        # Key: (tenant_id, user_id, agent_scope/session_id, cache_key).
        self._result_cache: dict[tuple[str, str, str, str], tuple[Any, float]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._cache_max_size = 200
        # Only idempotent tools are cacheable
        self._cacheable_prefixes = ("search_knowledge_base",)

    @staticmethod
    def _cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
        raw = tool_name + ":" + _json.dumps(arguments, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _is_cacheable(self, tool_name: str) -> bool:
        return any(tool_name.startswith(p) for p in self._cacheable_prefixes)

    @staticmethod
    def _cache_scope(context: ToolInvocationContext) -> tuple[str, str, str]:
        return (
            str(context.tenant_id or ""),
            str(context.user_id or ""),
            str(context.scope_id or context.session_id or ""),
        )

    def _cache_get(self, scope: tuple[str, str, str], key: str) -> Any | None:
        scoped_key = (*scope, key)
        entry = self._result_cache.get(scoped_key)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del self._result_cache[scoped_key]
        return None

    def _cache_put(self, scope: tuple[str, str, str], key: str, result: Any) -> None:
        if len(self._result_cache) >= self._cache_max_size:
            oldest = min(self._result_cache, key=lambda k: self._result_cache[k][1])
            del self._result_cache[oldest]
        self._result_cache[(*scope, key)] = (result, time.monotonic() + self._cache_ttl)

    async def _load_policy_snapshot(
        self,
        context: ToolInvocationContext,
        *,
        fresh: bool = False,
    ) -> ToolPolicySnapshot:
        """Resolve both tenant policy dimensions into one immutable value."""

        tenant_id = str(context.tenant_id or "")
        user_id = str(context.user_id or "")
        session_id = str(context.session_id or "")
        run_scope = str(context.run_id or context.request_id or "")
        if not tenant_id or not user_id or not session_id or not run_scope:
            return ToolPolicySnapshot.denied_for(context)

        tool_policy_resolved = True
        allowed_tools: frozenset[str] = frozenset()
        blocked_tools: frozenset[str] = frozenset()
        allowed_categories: frozenset[str] = frozenset()
        if self.tenant_tool_policy is not None:
            try:
                if fresh and hasattr(self.tenant_tool_policy, "get_policy_fresh"):
                    policy = await self.tenant_tool_policy.get_policy_fresh(tenant_id)
                else:
                    policy = await self.tenant_tool_policy.get_policy(tenant_id)
                allowed_tools = frozenset(str(item) for item in policy.allowed_tools)
                blocked_tools = frozenset(str(item) for item in policy.blocked_tools)
                allowed_categories = frozenset(str(item) for item in policy.allowed_categories)
            except Exception as exc:
                logger.warning(
                    "Tenant tool policy snapshot failed closed (exception_type=%s)",
                    type(exc).__name__,
                )
                tool_policy_resolved = False

        mcp_policy_resolved = True
        allowed_mcp_servers: frozenset[str] = frozenset()
        mcp_policy_source = "not_configured"
        if self.tenant_mcp_config is not None:
            try:
                if fresh and hasattr(self.tenant_mcp_config, "get_config_fresh"):
                    mcp_config = await self.tenant_mcp_config.get_config_fresh(tenant_id)
                else:
                    mcp_config = await self.tenant_mcp_config.get_config(tenant_id)
                allowed_mcp_servers = frozenset(str(item) for item in mcp_config.allowed_servers)
                mcp_policy_source = str(
                    getattr(mcp_config, "policy_source", "configured") or "configured"
                )
            except Exception as exc:
                logger.warning(
                    "Tenant MCP policy snapshot failed closed (exception_type=%s)",
                    type(exc).__name__,
                )
                mcp_policy_resolved = False
                mcp_policy_source = "unavailable"

        return ToolPolicySnapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_scope=run_scope,
            tool_policy_enabled=self.tenant_tool_policy is not None,
            tool_policy_resolved=tool_policy_resolved,
            allowed_tools=allowed_tools,
            blocked_tools=blocked_tools,
            allowed_categories=allowed_categories,
            mcp_policy_enabled=self.tenant_mcp_config is not None,
            mcp_policy_resolved=mcp_policy_resolved,
            allowed_mcp_servers=allowed_mcp_servers,
            mcp_policy_source=mcp_policy_source,
        )

    async def _policy_snapshots(
        self,
        context: ToolInvocationContext,
        *,
        fresh_current: bool = False,
    ) -> tuple[ToolPolicySnapshot, ToolPolicySnapshot]:
        """Return catalog ceiling plus a live, non-expanding revocation check."""

        pinned = context.policy_snapshot
        if pinned is not None and not pinned.matches(context):
            logger.warning("Tool policy snapshot identity mismatch; denying scope")
            denied = ToolPolicySnapshot.denied_for(context)
            context.policy_snapshot = denied
            return denied, denied
        if pinned is None:
            pinned = await self._load_policy_snapshot(context)
            context.policy_snapshot = pinned
            if not fresh_current:
                return pinned, pinned
        current = await self._load_policy_snapshot(
            context,
            fresh=fresh_current,
        )
        return pinned, current

    @staticmethod
    def _policy_allows(
        snapshots: tuple[ToolPolicySnapshot, ToolPolicySnapshot],
        *,
        tool_name: str,
        category: str | None,
        binding_type: str,
    ) -> bool:
        return all(
            snapshot.allows(
                tool_name,
                category=category,
                binding_type=binding_type,
            )
            for snapshot in snapshots
        )

    @staticmethod
    def _policy_metadata(
        snapshots: tuple[ToolPolicySnapshot, ToolPolicySnapshot],
    ) -> dict[str, Any]:
        pinned, current = snapshots
        return {
            "tool_policy_snapshot_id": pinned.snapshot_id,
            "tool_policy_recheck_id": current.snapshot_id,
            "tool_policy_revalidated": pinned.snapshot_id == current.snapshot_id,
        }

    def _tool_execution_policy(
        self,
        *,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        tool_definition: Any | None,
        logical_operation_id: str,
        binding_type: str = "",
    ) -> ToolExecutionPolicy:
        metadata = (
            dict(getattr(tool_definition, "capability_metadata", None) or {})
            if tool_definition is not None
            else {}
        )
        declared_kind = str(metadata.get("operation_kind") or "").lower()
        if declared_kind not in {"read", "write", "unknown"}:
            if tool_definition is None:
                # A legacy execution adapter may expose ``execute`` without a
                # model-facing ToolDefinition. Concrete ToolRegistry misses
                # still fail as unknown tools at dispatch. Dynamic MCP and
                # connector bindings remain conservative writes because their
                # side effects are external and learned at authorization time.
                declared_kind = "write" if binding_type in {"mcp", "connector"} else "read"
            elif bool(metadata.get("read_only")) or self._is_cacheable(tool_name):
                declared_kind = "read"
            elif bool(getattr(tool_definition, "requires_confirmation", False)) or str(
                getattr(getattr(tool_definition, "risk_level", None), "value", "low")
            ) in {"medium", "high"}:
                # ToolDefinition risk is repository-owned metadata: medium/high
                # explicitly means the tool may mutate data. Conservatively
                # fence an ambiguous timeout as an unknown write outcome.
                declared_kind = "write"
            else:
                declared_kind = "unknown"
        scope = ":".join(
            [
                context.tenant_id,
                context.user_id,
                context.session_id,
                str(context.run_id or context.request_id),
                tool_name,
            ]
        )
        encoded_args = _json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = hashlib.sha256(f"{scope}:{encoded_args}".encode()).hexdigest()
        digest = hashlib.sha256(f"{fingerprint}:{logical_operation_id}".encode()).hexdigest()
        operation_id = f"tool_op_{digest[:24]}"
        idempotency_supported = bool(metadata.get("idempotency_supported"))
        supplied_key = str((context.metadata or {}).get("idempotency_key") or "")
        idempotency_key = (
            supplied_key or f"tool_idem_{digest[24:56]}" if idempotency_supported else None
        )
        definition_retries = int(getattr(tool_definition, "max_retries", 0) or 0)
        retries = max(0, min(2, int(context.max_retries or 0), definition_retries))
        safe_to_retry = declared_kind == "read" or bool(idempotency_supported and idempotency_key)
        return ToolExecutionPolicy(
            operation_kind=declared_kind,
            operation_id=operation_id,
            operation_fingerprint=f"tool_fp_{fingerprint[:24]}",
            external_service=bool(metadata.get("external_service")),
            idempotency_key=idempotency_key,
            idempotency_supported=idempotency_supported,
            read_back_available=bool(metadata.get("read_back_available")),
            compensation_available=bool(metadata.get("compensation_available")),
            max_attempts=1 + retries if safe_to_retry else 1,
        )

    @staticmethod
    def _side_effect_unknown_metadata(
        policy: ToolExecutionPolicy,
        *,
        cause: str,
    ) -> dict[str, Any]:
        from .turn_contract import decide_failure

        decision = decide_failure("side_effect_unknown", side_effect_state="unknown").to_dict()
        if policy.read_back_available:
            decision["recovery_action"] = "resume"
        elif policy.compensation_available:
            decision["recovery_action"] = "compensate"
        return {
            "tool_operation": policy.to_dict(),
            "tool_failure": {**decision, "cause": cause},
            "side_effect_unknown": True,
        }

    @staticmethod
    def _result_has_unknown_side_effect(result: Any) -> bool:
        metadata = dict(getattr(result, "metadata", None) or {})
        if metadata.get("side_effect_unknown"):
            return True
        tool_failure = metadata.get("tool_failure") or {}
        mcp_failure = metadata.get("mcp_failure") or {}
        return any(
            isinstance(value, dict)
            and (
                value.get("side_effect_state") == "unknown"
                or value.get("failure_kind") == "side_effect_unknown"
            )
            for value in (tool_failure, mcp_failure)
        )

    def _side_effect_unresolved_metadata(
        self,
        policy: ToolExecutionPolicy,
        *,
        cause: str,
    ) -> dict[str, Any]:
        metadata = self._side_effect_unknown_metadata(policy, cause=cause)
        metadata["side_effect_unresolved"] = True
        return metadata

    async def _deny_tool_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        error: str,
        start_time: float,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Return a denied tool result and record best-effort audit evidence."""
        from .tools.tool_registry import ToolCallResult

        duration_ms = (time.time() - start_time) * 1000
        safe_error = _safe_public_error(error)
        tool_label = _tool_log_label(tool_name)
        if self.tool_audit:
            try:
                from .audit.tool_audit import ToolAuditEntry

                await self.tool_audit.log(
                    ToolAuditEntry(
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        request_id=context.request_id,
                        tool_type=self.tool_audit.classify_tool_type(tool_name),
                        tool_name=tool_name,
                        input_summary=self.tool_audit.summarize_input(arguments),
                        output_status="denied",
                        error_message=safe_error,
                        latency_ms=duration_ms,
                    )
                )
            except Exception as exc:
                logger.debug(
                    "Denied tool audit failed (tool_label=%s, exception_type=%s)",
                    tool_label,
                    type(exc).__name__,
                )

        return ToolCallResult(
            call_id=call_id,
            tool_name=tool_name,
            success=False,
            error=safe_error,
            duration_ms=duration_ms,
            metadata=dict(metadata or {}),
        )

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolCallResult:
        """
        Invoke a tool with the given arguments and context.

        Execution flow:
        1. Check cancellation
        2. Check rate limit (if configured)
        3. Build ToolCallRequest
        4. Execute with timeout and cancellation support
        5. Retry on transient failures
        6. Record metrics (if configured)
        7. Return result
        """
        from .tools.tool_registry import (
            ToolCallRequest,
            ToolCallResult,
            ToolDefinition,
            validate_tool_arguments,
        )

        start_time = time.time()
        call_id = str(uuid.uuid4())
        tool_label = _tool_log_label(tool_name)
        runtime_registry = context.runtime_tool_registry
        runtime_definition = (
            runtime_registry.get_tool(tool_name) if runtime_registry is not None else None
        )
        execution_registry = (
            runtime_registry if runtime_definition is not None else self.tool_registry
        )
        tool_definition = runtime_definition or self.tool_registry.get_tool(tool_name)
        if not isinstance(tool_definition, ToolDefinition):
            tool_definition = None

        # Check cancellation before starting
        if cancel_event and cancel_event.is_set():
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error="Cancelled before execution",
            )

        if context.capability_allowlist is not None and not context.capability_allowlist.allows(
            tool_name
        ):
            logger.warning(
                "Agent capability allowlist denied (tool_label=%s)",
                tool_label,
            )
            return await self._deny_tool_call(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                context=context,
                error=f"Tool '{tool_name}' is not available to this Agent.",
                start_time=start_time,
            )

        capability_binding = (
            context.capability_allowlist.binding(tool_name)
            if context.capability_allowlist is not None
            else None
        )
        binding_type = str((capability_binding or {}).get("type") or "")
        policy_snapshots = await self._policy_snapshots(
            context,
            fresh_current=True,
        )
        policy_metadata = self._policy_metadata(policy_snapshots)
        tool_category = (
            tool_definition.category.value
            if tool_definition is not None
            else "mcp"
            if binding_type == "mcp"
            else None
        )
        if not self._policy_allows(
            policy_snapshots,
            tool_name=tool_name,
            category=tool_category,
            binding_type=binding_type,
        ):
            logger.warning(
                "Tool policy snapshot denied (tool_label=%s)",
                tool_label,
            )
            return await self._deny_tool_call(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                context=context,
                error=f"Tool '{tool_name}' is not available for this tenant.",
                start_time=start_time,
                metadata=policy_metadata,
            )
        if binding_type == "mcp" and tool_definition is None:
            if self.mcp_runtime is None or capability_binding is None:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="MCP capability is unavailable.",
                    start_time=start_time,
                    metadata=policy_metadata,
                )
            try:
                dynamic_definitions = await self.mcp_runtime.get_tool_definitions(
                    context=context,
                    bindings={tool_name: capability_binding},
                    tool_names={tool_name},
                )
            except Exception:
                dynamic_definitions = []
            tool_definition = next(
                (
                    definition
                    for definition in dynamic_definitions
                    if isinstance(definition, ToolDefinition) and definition.name == tool_name
                ),
                None,
            )
            if tool_definition is None:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="MCP capability schema is unavailable.",
                    start_time=start_time,
                    metadata=policy_metadata,
                )
        connector_authorization: dict[str, Any] | None = None
        if binding_type == "connector":
            if self.mcp_runtime is None:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="Connector capability is unavailable.",
                    start_time=start_time,
                )
            try:
                connector_authorization = await self.mcp_runtime.authorize_connector_binding(
                    tool_name=tool_name,
                    binding=capability_binding,
                    context=context,
                )
            except Exception:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="Connector capability is unavailable.",
                    start_time=start_time,
                )
        effective_arguments = arguments
        if context.capability_allowlist is not None and tool_name == "search_knowledge_base":
            allowed_dataset_ids = frozenset(str(value) for value in context.kb_dataset_ids)
            requested_dataset_ids = arguments.get("dataset_ids")
            if not allowed_dataset_ids:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="Knowledge base search is not available to this Agent.",
                    start_time=start_time,
                )
            if requested_dataset_ids:
                if not isinstance(requested_dataset_ids, (list, tuple, set)):
                    return await self._deny_tool_call(
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        context=context,
                        error="Knowledge dataset is not available to this Agent.",
                        start_time=start_time,
                    )
                requested = frozenset(str(value) for value in requested_dataset_ids)
                if not requested.issubset(allowed_dataset_ids):
                    return await self._deny_tool_call(
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        context=context,
                        error="Knowledge dataset is not available to this Agent.",
                        start_time=start_time,
                    )
                effective_arguments = {
                    **arguments,
                    "dataset_ids": sorted(requested),
                }
            else:
                effective_arguments = {
                    **arguments,
                    "dataset_ids": sorted(allowed_dataset_ids),
                }

        validation_receipt: dict[str, Any] | None = None
        if tool_definition is not None:
            validation_receipt = validate_tool_arguments(
                tool_definition,
                arguments,
            )
        elif bool((context.metadata or {}).get("model_generated")):
            validation_receipt = {
                "schema_version": "assistant-tool-arguments/v1",
                "schema_sha256": "",
                "valid": False,
                "code": "schema_unavailable",
                "issue_count": 0,
                "issues": [],
            }
        if validation_receipt is not None and not validation_receipt["valid"]:
            validation_receipt = {
                **validation_receipt,
                "correction_supported": True,
            }
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                result=_json.dumps(
                    {
                        "error": {
                            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
                            "validation": validation_receipt,
                        }
                    },
                    separators=(",", ":"),
                ),
                error="TOOL_ARGUMENT_VALIDATION_FAILED",
                duration_ms=(time.time() - start_time) * 1000,
                metadata={
                    **policy_metadata,
                    "tool_argument_validation": validation_receipt,
                },
            )

        # ADR-003 Phase 3: Check result cache for idempotent tools
        cache_key = None
        cached = None
        cache_scope = self._cache_scope(context)
        if self._is_cacheable(tool_name):
            cache_args = effective_arguments
            if tool_name == "search_knowledge_base":
                cache_args = {
                    **effective_arguments,
                    "dataset_ids": effective_arguments.get("dataset_ids") or context.kb_dataset_ids,
                    "_sealed_retrieval_configs": (
                        (context.metadata or {}).get("kb_retrieval_configs") or {}
                    ),
                }
            cache_key = self._cache_key(tool_name, cache_args)
            cached = self._cache_get(cache_scope, cache_key)

        # Authorization is deliberately evaluated before a cache hit can be
        # returned. A stale cached result must not bypass a newly denied or
        # currently unavailable tenant/MCP policy.
        if cached is not None:
            logger.info("Tool cache hit (tool_label=%s)", tool_label)
            cached_copy = ToolCallResult(
                call_id=call_id,
                tool_name=cached.tool_name,
                success=cached.success,
                result=cached.result,
                error=(_safe_public_error(cached.error) if cached.error is not None else None),
                duration_ms=0,
                metadata={
                    **cached.metadata,
                    **policy_metadata,
                    **(
                        {"tool_argument_validation": validation_receipt}
                        if validation_receipt is not None
                        else {}
                    ),
                    "cache_hit": True,
                },
                output_files=cached.output_files,
            )
            if self.tool_audit:
                try:
                    from .audit.tool_audit import ToolAuditEntry

                    entry = ToolAuditEntry(
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        request_id=context.request_id,
                        tool_type=self.tool_audit.classify_tool_type(tool_name),
                        tool_name=tool_name,
                        input_summary=self.tool_audit.summarize_input(arguments),
                        output_status="cache_hit",
                        latency_ms=0,
                    )
                    _t = asyncio.create_task(self.tool_audit.log(entry))
                    _t.add_done_callback(lambda task: _log_audit_task_completion(task, tool_label))
                except Exception as exc:
                    logger.debug(
                        "Cached tool audit setup failed (tool_label=%s, exception_type=%s)",
                        tool_label,
                        type(exc).__name__,
                    )
            return cached_copy

        # Check rate limit
        if self.rate_limiter and self.rate_limiter(context.tenant_id, tool_name):
            logger.warning("Tool invocation rate limited (tool_label=%s)", tool_label)
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error="Rate limit exceeded. Please try again later.",
                metadata={
                    **(
                        {"tool_argument_validation": validation_receipt}
                        if validation_receipt is not None
                        else {}
                    )
                },
            )

        # Auto-inject kb_dataset_ids for KB search tool if not provided
        # This fixes the issue where LLM calls the tool without knowing which datasets to search
        final_arguments = effective_arguments.copy()
        if (
            tool_name == "search_knowledge_base"
            and not final_arguments.get("dataset_ids")
            and context.kb_dataset_ids
        ):
            final_arguments["dataset_ids"] = context.kb_dataset_ids
            logger.info(
                "Injected knowledge dataset scope (tool_label=%s, dataset_count=%s)",
                tool_label,
                len(context.kb_dataset_ids),
            )

        execution_policy = self._tool_execution_policy(
            context=context,
            tool_name=tool_name,
            arguments=final_arguments,
            tool_definition=tool_definition,
            logical_operation_id=str(
                (context.metadata or {}).get("logical_operation_id") or call_id
            ),
            binding_type=binding_type,
        )

        fingerprint = execution_policy.operation_fingerprint
        operation_claimed = False
        if execution_policy.side_effecting:
            if fingerprint in context.uncertain_operation_fingerprints:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="SIDE_EFFECT_UNRESOLVED",
                    start_time=start_time,
                    metadata={
                        **self._side_effect_unresolved_metadata(
                            execution_policy,
                            cause="previous_unresolved_operation",
                        ),
                        **(
                            {"tool_argument_validation": validation_receipt}
                            if validation_receipt is not None
                            else {}
                        ),
                    },
                )
            if fingerprint in context.inflight_operation_fingerprints:
                return await self._deny_tool_call(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    error="SIDE_EFFECT_UNRESOLVED",
                    start_time=start_time,
                    metadata={
                        **self._side_effect_unresolved_metadata(
                            execution_policy,
                            cause="operation_in_flight",
                        ),
                        **(
                            {"tool_argument_validation": validation_receipt}
                            if validation_receipt is not None
                            else {}
                        ),
                    },
                )
            context.inflight_operation_fingerprints.add(fingerprint)
            operation_claimed = True

        # Build request
        request = ToolCallRequest(
            call_id=call_id,
            tool_name=tool_name,
            arguments=final_arguments,
            user=context.user,  # Pass user context for tools that need permissions
            metadata={
                **(context.metadata or {}),
                **policy_metadata,
                "session_id": context.session_id,
                "user_id": context.user_id,
                "tenant_id": context.tenant_id,
                "request_id": context.request_id,
                "run_id": context.run_id,
                "scope_id": context.scope_id,
                "policy_profile": context.policy_profile,
                "kb_dataset_ids": context.kb_dataset_ids,
                "tool_operation": execution_policy.to_dict(),
                "idempotency_key": execution_policy.idempotency_key,
                **(
                    {
                        "connector_principal": {
                            "grant_id": str(connector_authorization.get("grant_id") or ""),
                            "provider": str(connector_authorization.get("provider") or ""),
                            "principal_type": str(
                                connector_authorization.get("principal_type") or ""
                            ),
                            "channel": str((context.metadata or {}).get("channel") or ""),
                        }
                    }
                    if connector_authorization is not None
                    else {}
                ),
            },
        )

        # Use tool-specific timeout if defined (e.g. quiz generation needs 120s)
        effective_timeout = context.timeout_ms
        tool_timeout_seconds = getattr(tool_definition, "timeout_seconds", None)
        if (
            isinstance(tool_timeout_seconds, (int, float))
            and not isinstance(tool_timeout_seconds, bool)
            and tool_timeout_seconds * 1000 > effective_timeout
        ):
            effective_timeout = tool_timeout_seconds * 1000

        # Execute with timeout, retry, and cancellation support
        try:
            if binding_type == "mcp":
                if self.mcp_runtime is None or capability_binding is None:
                    result = ToolCallResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        success=False,
                        error="MCP capability is unavailable.",
                    )
                else:
                    mcp_task = asyncio.create_task(
                        self.mcp_runtime.invoke(
                            tool_name=tool_name,
                            arguments=final_arguments,
                            binding=capability_binding,
                            context=context,
                            call_id=call_id,
                        )
                    )
                    cancel_task: asyncio.Task[bool] | None = None
                    try:
                        if cancel_event is None:
                            result = await mcp_task
                        else:
                            cancel_task = asyncio.create_task(cancel_event.wait())
                            done, _pending = await asyncio.wait(
                                {mcp_task, cancel_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if mcp_task in done:
                                result = mcp_task.result()
                            else:
                                mcp_task.cancel()
                                try:
                                    result = await mcp_task
                                except asyncio.CancelledError:
                                    result = ToolCallResult(
                                        call_id=call_id,
                                        tool_name=tool_name,
                                        success=False,
                                        error="Cancelled before MCP dispatch",
                                    )
                    except asyncio.CancelledError:
                        mcp_task.cancel()
                        try:
                            result = await mcp_task
                        except asyncio.CancelledError:
                            result = ToolCallResult(
                                call_id=call_id,
                                tool_name=tool_name,
                                success=False,
                                error="Cancelled before MCP dispatch",
                            )
                    finally:
                        if cancel_task is not None and not cancel_task.done():
                            cancel_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await cancel_task
            else:
                result = await self._execute_with_retry(
                    request=request,
                    timeout_ms=effective_timeout,
                    execution_policy=execution_policy,
                    cancel_event=cancel_event,
                    tool_registry=execution_registry,
                )
            if operation_claimed and self._result_has_unknown_side_effect(result):
                context.uncertain_operation_fingerprints.add(fingerprint)
        finally:
            if operation_claimed:
                context.inflight_operation_fingerprints.discard(fingerprint)

        if result.error is not None:
            result.error = _safe_public_error(result.error)

        # Record metrics
        duration_ms = (time.time() - start_time) * 1000
        if self.metrics_collector:
            try:
                self.metrics_collector(tool_name, duration_ms, result.success)
            except Exception as exc:
                logger.error(
                    "Tool metrics collection failed (tool_label=%s, exception_type=%s)",
                    tool_label,
                    type(exc).__name__,
                )

        # Audit log (fire-and-forget with error suppression)
        if self.tool_audit:
            try:
                from .audit.tool_audit import ToolAuditEntry

                entry = ToolAuditEntry(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    request_id=context.request_id,
                    tool_type=self.tool_audit.classify_tool_type(tool_name),
                    tool_name=tool_name,
                    input_summary=self.tool_audit.summarize_input(arguments),
                    output_status="success" if result.success else "error",
                    error_message=result.error if not result.success else None,
                    latency_ms=duration_ms,
                )
                task = asyncio.create_task(self.tool_audit.log(entry))
                task.add_done_callback(
                    lambda finished: _log_audit_task_completion(finished, tool_label)
                )
            except Exception as exc:
                logger.debug(
                    "Tool audit setup failed (tool_label=%s, exception_type=%s)",
                    tool_label,
                    type(exc).__name__,
                )

        # ADR-003 Phase 3: Cache successful results for idempotent tools
        if cache_key and result.success:
            self._cache_put(cache_scope, cache_key, result)

        result.metadata = {
            **(result.metadata or {}),
            **policy_metadata,
            **(
                {"tool_argument_validation": validation_receipt}
                if validation_receipt is not None
                else {}
            ),
        }

        return result

    async def _execute_with_retry(
        self,
        request: ToolCallRequest,
        timeout_ms: int,
        execution_policy: ToolExecutionPolicy,
        cancel_event: asyncio.Event | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> ToolCallResult:
        """
        Execute with timeout, retry, and cancellation support.

        Retries on:
        - Timeout errors
        - Transient network errors

        Does NOT retry on:
        - Validation errors
        - Tool not found
        - Business logic errors
        - Cancellation
        """
        from .tools.tool_registry import ToolCallResult

        last_error: str | None = None
        execution_registry = tool_registry or self.tool_registry

        max_attempts = execution_policy.max_attempts
        for attempt in range(max_attempts):
            # Check cancellation before each attempt
            if cancel_event and cancel_event.is_set():
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="Cancelled during execution",
                )

            execution_task: asyncio.Task[ToolCallResult] | None = None
            try:
                # Create execution task
                execution_task = asyncio.create_task(execution_registry.execute(request))

                # If we have a cancel event, race between execution and cancellation
                if cancel_event:
                    cancel_task = asyncio.create_task(cancel_event.wait())

                    done, pending = await asyncio.wait(
                        [execution_task, cancel_task],
                        timeout=timeout_ms / 1000,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Cancel any pending tasks
                    for task in pending:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

                    # Check if cancellation won the race
                    if cancel_task in done:
                        if execution_policy.may_have_external_side_effect:
                            return ToolCallResult(
                                call_id=request.call_id,
                                tool_name=request.tool_name,
                                success=False,
                                error="SIDE_EFFECT_UNKNOWN",
                                metadata=self._side_effect_unknown_metadata(
                                    execution_policy,
                                    cause="cancelled_after_dispatch",
                                ),
                            )
                        return ToolCallResult(
                            call_id=request.call_id,
                            tool_name=request.tool_name,
                            success=False,
                            error="Cancelled during execution",
                        )

                    # Check timeout (no tasks in done means timeout)
                    if not done:
                        raise asyncio.TimeoutError()

                    # Get the execution result
                    result = execution_task.result()
                else:
                    # No cancel event - simple wait_for
                    result = await asyncio.wait_for(
                        execution_task,
                        timeout=timeout_ms / 1000,
                    )

                # A tool-level timeout may have happened after a write was
                # accepted. Only explicitly read-only/idempotent operations
                # can be replayed; every other result pauses as unknown.
                if not result.success:
                    error_lower = (result.error or "").lower()
                    if any(
                        key in (result.metadata or {})
                        for key in ("mcp_failure", "tool_failure", "side_effect_state")
                    ):
                        return result
                    if (
                        "cancelled" in error_lower
                        and execution_policy.may_have_external_side_effect
                    ):
                        result.metadata = {
                            **(result.metadata or {}),
                            **self._side_effect_unknown_metadata(
                                execution_policy,
                                cause="cancelled_after_dispatch",
                            ),
                        }
                        result.error = "SIDE_EFFECT_UNKNOWN"
                        return result
                    if any(
                        phrase in error_lower
                        for phrase in [
                            "unknown tool",
                            "validation error",
                            "missing required",
                            "permission denied",
                            "requires the assistantexecutiongateway",
                            "cancelled",
                        ]
                    ):
                        return result
                    is_timeout = "timed out" in error_lower or "timeout" in error_lower
                    if (
                        is_timeout
                        and execution_policy.may_have_external_side_effect
                        and not (execution_policy.replay_safe and attempt + 1 < max_attempts)
                    ):
                        result.metadata = {
                            **(result.metadata or {}),
                            **self._side_effect_unknown_metadata(
                                execution_policy,
                                cause="deadline",
                            ),
                        }
                        result.error = "SIDE_EFFECT_UNKNOWN"
                        return result
                    if is_timeout and attempt + 1 < max_attempts:
                        continue
                    if execution_policy.may_have_external_side_effect:
                        result.metadata = {
                            **(result.metadata or {}),
                            **self._side_effect_unknown_metadata(
                                execution_policy,
                                cause="untyped_external_failure",
                            ),
                        }
                        result.error = "SIDE_EFFECT_UNKNOWN"
                        return result

                return result

            except asyncio.TimeoutError:
                last_error = f"Tool execution timed out after {timeout_ms}ms"
                logger.warning(
                    "Tool execution timed out (tool_label=%s, attempt=%s, max_attempts=%s)",
                    _tool_log_label(request.tool_name),
                    attempt + 1,
                    max_attempts,
                )
                if execution_policy.may_have_external_side_effect and not (
                    execution_policy.replay_safe and attempt + 1 < max_attempts
                ):
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="SIDE_EFFECT_UNKNOWN",
                        metadata=self._side_effect_unknown_metadata(
                            execution_policy,
                            cause="host_deadline",
                        ),
                    )

            except asyncio.CancelledError:
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await execution_task
                if execution_policy.may_have_external_side_effect:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="SIDE_EFFECT_UNKNOWN",
                        metadata=self._side_effect_unknown_metadata(
                            execution_policy,
                            cause="cancelled_after_dispatch",
                        ),
                    )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="Task cancelled",
                )

            except Exception as exc:
                last_error = _safe_public_error(exc)
                logger.warning(
                    "Tool execution attempt failed "
                    "(tool_label=%s, attempt=%s, max_attempts=%s, exception_type=%s)",
                    _tool_log_label(request.tool_name),
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                )
                if execution_policy.may_have_external_side_effect and not (
                    execution_policy.replay_safe and attempt + 1 < max_attempts
                ):
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="SIDE_EFFECT_UNKNOWN",
                        metadata=self._side_effect_unknown_metadata(
                            execution_policy,
                            cause="transport",
                        ),
                    )

            # Wait before retry (exponential backoff), but check cancellation
            if attempt + 1 < max_attempts:
                if cancel_event and cancel_event.is_set():
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="Cancelled during retry wait",
                    )
                await asyncio.sleep(0.5 * (2**attempt))

        # All retries exhausted
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=False,
            error=_safe_public_error(f"Failed after {max_attempts} attempts: {last_error}"),
            metadata={"tool_operation": execution_policy.to_dict()},
        )

    async def invoke_batch(
        self,
        requests: list[dict[str, Any]],
        context: ToolInvocationContext,
        parallel: bool = True,
        max_concurrency: int = 5,
    ) -> BatchInvocationResult:
        """
        Invoke multiple tools, optionally in parallel.

        When parallel=True, uses a semaphore to limit concurrency.
        Results are returned in the same order as requests.
        """
        start_time = time.time()

        if not requests:
            return BatchInvocationResult(
                results=[],
                total_duration_ms=0,
                successful_count=0,
                failed_count=0,
            )

        if parallel:
            results = await self._invoke_parallel(
                requests=requests,
                context=context,
                max_concurrency=max_concurrency,
            )
        else:
            results = await self._invoke_sequential(
                requests=requests,
                context=context,
            )

        total_duration_ms = (time.time() - start_time) * 1000
        successful_count = sum(1 for r in results if r.success)
        failed_count = len(results) - successful_count

        return BatchInvocationResult(
            results=results,
            total_duration_ms=total_duration_ms,
            successful_count=successful_count,
            failed_count=failed_count,
        )

    async def _invoke_parallel(
        self,
        requests: list[dict[str, Any]],
        context: ToolInvocationContext,
        max_concurrency: int,
    ) -> list[ToolCallResult]:
        """Execute requests in parallel with concurrency limit."""
        semaphore = asyncio.Semaphore(max_concurrency)

        async def invoke_with_semaphore(req: dict[str, Any], idx: int):
            async with semaphore:
                result = await self.invoke(
                    tool_name=req["tool_name"],
                    arguments=req.get("arguments", {}),
                    context=context,
                )
                return (idx, result)

        # Create tasks preserving order
        tasks = [invoke_with_semaphore(req, idx) for idx, req in enumerate(requests)]

        # Execute all and collect results
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        # Sort by original index and extract results

        results: list[ToolCallResult] = [None] * len(requests)  # type: ignore
        for completed_index, item in enumerate(completed):
            if isinstance(item, Exception):
                # Should not happen with return_exceptions=True
                tool_name = requests[completed_index].get("tool_name", "unknown")
                logger.error(
                    "Unexpected tool batch exception (tool_label=%s, exception_type=%s)",
                    _tool_log_label(tool_name),
                    type(item).__name__,
                )
                continue
            idx, result = item
            results[idx] = result

        return results

    async def _invoke_sequential(
        self,
        requests: list[dict[str, Any]],
        context: ToolInvocationContext,
    ) -> list[ToolCallResult]:
        """Execute requests sequentially."""
        from .tools.tool_registry import ToolCallResult

        results = []
        for index, req in enumerate(requests):
            result = await self.invoke(
                tool_name=req["tool_name"],
                arguments=req.get("arguments", {}),
                context=context,
            )
            results.append(result)
            if self._result_has_unknown_side_effect(result):
                results.extend(
                    ToolCallResult(
                        call_id=str(uuid.uuid4()),
                        tool_name=str(pending.get("tool_name") or "unknown"),
                        success=False,
                        error="SIDE_EFFECT_UNRESOLVED",
                        metadata={
                            "side_effect_unresolved": True,
                            "blocked_by_batch_index": index,
                        },
                    )
                    for pending in requests[index + 1 :]
                )
                break
        return results

    def get_available_tools(
        self,
        context: ToolInvocationContext,
    ) -> list[str]:
        """Get list of tool names available for this context."""
        tools = self._context_tools(context)
        if context.capability_allowlist is not None:
            tools = context.capability_allowlist.filter_definitions(tools)
        return [t.name for t in tools]

    def get_tool_definitions(
        self,
        context: ToolInvocationContext,
        tool_names: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """Get tool definitions for schema generation."""
        tools = self._context_tools(context)
        if tool_names:
            tools = [t for t in tools if t.name in tool_names]
        if context.capability_allowlist is not None:
            tools = context.capability_allowlist.filter_definitions(tools)
        return tools

    def _context_tools(
        self,
        context: ToolInvocationContext,
    ) -> list[ToolDefinition]:
        """Merge global platform tools with this run's isolated tool overlay."""

        merged = {tool.name: tool for tool in self.tool_registry.list_tools(user=context.user)}
        runtime_registry = context.runtime_tool_registry
        if runtime_registry is not None:
            for tool in runtime_registry.list_tools(user=context.user):
                merged[tool.name] = tool
        return list(merged.values())

    async def get_tool_definitions_filtered(
        self,
        context: ToolInvocationContext,
        tool_names: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """Get tool definitions with per-tenant policy + MCP filtering (async).

        Falls back to `get_tool_definitions()` if no policies are configured.
        """
        tools = self.get_tool_definitions(context, tool_names)
        policy_snapshots = await self._policy_snapshots(
            context,
            fresh_current=True,
        )

        if self.mcp_runtime and context.capability_allowlist is not None:
            requested = set(context.capability_allowlist.tool_names)
            if tool_names is not None:
                requested.intersection_update(tool_names)
            dynamic = await self.mcp_runtime.get_tool_definitions(
                context=context,
                bindings=context.capability_allowlist.bindings,
                tool_names=requested,
            )
            known = {tool.name for tool in tools}
            tools.extend(tool for tool in dynamic if tool.name not in known)
            denied_connectors: set[str] = set()
            for name, binding in context.capability_allowlist.bindings.items():
                if str(binding.get("type") or "") != "connector":
                    continue
                try:
                    await self.mcp_runtime.authorize_connector_binding(
                        tool_name=name,
                        binding=binding,
                        context=context,
                    )
                except Exception:
                    denied_connectors.add(name)
            if denied_connectors:
                tools = [tool for tool in tools if tool.name not in denied_connectors]

        def _binding_type(name: str) -> str:
            if context.capability_allowlist is None:
                return ""
            binding = context.capability_allowlist.bindings.get(name) or {}
            return str(binding.get("type") or "")

        tools = [
            tool
            for tool in tools
            if self._policy_allows(
                policy_snapshots,
                tool_name=tool.name,
                category=tool.category.value,
                binding_type=_binding_type(tool.name),
            )
        ]

        return tools

    async def filter_tool_definitions_authorized(
        self,
        context: ToolInvocationContext,
        tools: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        """Apply pinned and fresh policy state to a secondary catalog source."""

        policy_snapshots = await self._policy_snapshots(
            context,
            fresh_current=True,
        )

        def _binding_type(name: str) -> str:
            if context.capability_allowlist is None:
                return ""
            binding = context.capability_allowlist.bindings.get(name) or {}
            return str(binding.get("type") or "")

        filtered = [
            tool
            for tool in tools
            if self._policy_allows(
                policy_snapshots,
                tool_name=tool.name,
                category=tool.category.value,
                binding_type=_binding_type(tool.name),
            )
        ]
        if context.capability_allowlist is not None:
            filtered = context.capability_allowlist.filter_definitions(filtered)
        return filtered


# =============================================================================
# Factory Functions
# =============================================================================


def create_tool_invoker(
    tool_registry: ToolRegistry | None = None,
    rate_limiter: Callable[[str, str], bool] | None = None,
    metrics_collector: Callable[[str, float, bool], None] | None = None,
    tenant_tool_policy: Any | None = None,
    tenant_mcp_config: Any | None = None,
    mcp_runtime: Any | None = None,
    tool_audit: Any | None = None,
) -> ToolInvoker:
    """
    Create a ToolInvoker instance.

    Args:
        tool_registry: Optional ToolRegistry (uses global if not provided)
        rate_limiter: Optional rate limiting callback
        metrics_collector: Optional metrics collection callback
        tenant_tool_policy: TenantToolPolicyService for per-tenant filtering
        tenant_mcp_config: TenantMCPConfigService for per-tenant MCP filtering
        tool_audit: ToolAuditService for audit logging

    Returns:
        Configured ToolInvoker instance
    """
    from .tools.tool_registry import get_tool_registry

    registry = tool_registry or get_tool_registry()
    if tenant_mcp_config is None:
        from .mcp.tenant_mcp_config import TenantMCPConfigService

        tenant_mcp_config = TenantMCPConfigService(database=None)

    return RegistryToolInvoker(
        tool_registry=registry,
        rate_limiter=rate_limiter,
        metrics_collector=metrics_collector,
        tenant_tool_policy=tenant_tool_policy,
        tenant_mcp_config=tenant_mcp_config,
        mcp_runtime=mcp_runtime,
        tool_audit=tool_audit,
    )


# Convenience alias
get_tool_invoker = create_tool_invoker
