"""Public invocation contracts shared by tool runtimes and gateways."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json as _json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike

    from .tools.tool_registry import ToolCallResult, ToolDefinition


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
        # Discovery bridges are not capabilities of their own: they can only
        # enumerate or route back into this exact allowlist. Keeping them
        # visible avoids baking platform meta-tool names into every Agent
        # Version while the underlying call remains strictly bounded here.
        from .tools.tool_discovery import is_tool_discovery_bridge

        if is_tool_discovery_bridge(tool_name):
            return True
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
        from .tools.tool_discovery import is_tool_discovery_bridge

        if is_tool_discovery_bridge(tool_name):
            return not (self.tool_policy_enabled and tool_name in self.blocked_tools)
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
