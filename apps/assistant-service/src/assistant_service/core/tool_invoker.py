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
import hashlib
import json as _json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike
    from .tools.tool_registry import ToolCallRequest, ToolCallResult, ToolDefinition, ToolRegistry

logger = get_logger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


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
        self.tool_audit = tool_audit

        # ADR-003 Phase 3: Per-session tool result cache
        # Key: (session_id, cache_key) → (ToolCallResult, expires_at)
        self._result_cache: dict[tuple[str, str], tuple[Any, float]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._cache_max_size = 200
        # Only idempotent tools are cacheable
        self._cacheable_prefixes = (
            "search_knowledge_base", "search_web",
            "mcp_halalmoney__get_", "mcp_halalmoney__check_",
            "mcp_wahda__search_",
        )

    @staticmethod
    def _cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
        raw = tool_name + ":" + _json.dumps(arguments, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _is_cacheable(self, tool_name: str) -> bool:
        return any(tool_name.startswith(p) for p in self._cacheable_prefixes)

    def _cache_get(self, session_id: str, key: str) -> Any | None:
        entry = self._result_cache.get((session_id, key))
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del self._result_cache[(session_id, key)]
        return None

    def _cache_put(self, session_id: str, key: str, result: Any) -> None:
        if len(self._result_cache) >= self._cache_max_size:
            oldest = min(self._result_cache, key=lambda k: self._result_cache[k][1])
            del self._result_cache[oldest]
        self._result_cache[(session_id, key)] = (result, time.monotonic() + self._cache_ttl)

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
        from .tools.tool_registry import ToolCallRequest, ToolCallResult

        start_time = time.time()
        call_id = str(uuid.uuid4())

        # Check cancellation before starting
        if cancel_event and cancel_event.is_set():
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error="Cancelled before execution",
            )

        # ADR-003 Phase 3: Check result cache for idempotent tools
        cache_key = None
        if self._is_cacheable(tool_name):
            # Include kb_dataset_ids in cache key for KB search (auto-injected later)
            cache_args = arguments
            if tool_name == "search_knowledge_base" and not arguments.get("dataset_ids") and context.kb_dataset_ids:
                cache_args = {**arguments, "dataset_ids": context.kb_dataset_ids}
            cache_key = self._cache_key(tool_name, cache_args)
            cached = self._cache_get(context.session_id, cache_key)
            if cached is not None:
                logger.info(f"Cache hit: tool={tool_name} session={context.session_id[:12]}")
                # Return a copy with fresh call_id
                cached_copy = ToolCallResult(
                    call_id=call_id,
                    tool_name=cached.tool_name,
                    success=cached.success,
                    result=cached.result,
                    error=cached.error,
                    duration_ms=0,
                    metadata={**cached.metadata, "cache_hit": True},
                    output_files=cached.output_files,
                )
                # Audit cache hit
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
                        _t.add_done_callback(lambda t: None if not t.exception() else None)
                    except Exception:
                        pass
                return cached_copy

        # Enforce tenant tool policy (blocked tools / MCP access)
        if self.tenant_tool_policy and context.tenant_id:
            try:
                policy = await self.tenant_tool_policy.get_policy(context.tenant_id)
                if tool_name in policy.blocked_tools:
                    logger.warning(f"Tenant policy denied: tool={tool_name} tenant={context.tenant_id}")
                    return ToolCallResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        success=False,
                        error=f"Tool '{tool_name}' is not available for this tenant.",
                    )
                if policy.allowed_tools and tool_name not in policy.allowed_tools:
                    logger.warning(f"Tenant policy denied (not in whitelist): tool={tool_name} tenant={context.tenant_id}")
                    return ToolCallResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        success=False,
                        error=f"Tool '{tool_name}' is not available for this tenant.",
                    )
                if policy.allowed_categories:
                    tool_def = self.tool_registry.get_tool(tool_name)
                    if tool_def and tool_def.category.value not in policy.allowed_categories:
                        logger.warning(f"Tenant policy denied (category): tool={tool_name} cat={tool_def.category.value} tenant={context.tenant_id}")
                        return ToolCallResult(
                            call_id=call_id,
                            tool_name=tool_name,
                            success=False,
                            error=f"Tool '{tool_name}' category is not allowed for this tenant.",
                        )
            except Exception as e:
                logger.warning(f"Tenant policy check failed (allowing): {e}")

        if self.tenant_mcp_config and context.tenant_id and tool_name.startswith("mcp_"):
            try:
                mcp_config = await self.tenant_mcp_config.get_config(context.tenant_id)
                allowed_prefixes = tuple(f"mcp_{s}__" for s in mcp_config.allowed_servers)
                if not tool_name.startswith(allowed_prefixes):
                    logger.warning(f"MCP policy denied: tool={tool_name} tenant={context.tenant_id}")
                    return ToolCallResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        success=False,
                        error=f"MCP tool '{tool_name}' is not available for this tenant.",
                    )
            except Exception as e:
                logger.warning(f"MCP policy check failed (allowing): {e}")

        # Check rate limit
        if self.rate_limiter and self.rate_limiter(context.tenant_id, tool_name):
            logger.warning(f"Rate limited: tool={tool_name} tenant={context.tenant_id}")
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error="Rate limit exceeded. Please try again later.",
            )

        # Auto-inject kb_dataset_ids for KB search tool if not provided
        # This fixes the issue where LLM calls the tool without knowing which datasets to search
        final_arguments = arguments.copy()
        if (
            tool_name == "search_knowledge_base"
            and not final_arguments.get("dataset_ids")
            and context.kb_dataset_ids
        ):
            final_arguments["dataset_ids"] = context.kb_dataset_ids
            logger.info(
                f"Auto-injected kb_dataset_ids into search_knowledge_base: {context.kb_dataset_ids}"
            )

        # Build request
        request = ToolCallRequest(
            call_id=call_id,
            tool_name=tool_name,
            arguments=final_arguments,
            user=context.user,  # Pass user context for tools that need permissions
            metadata={
                "session_id": context.session_id,
                "user_id": context.user_id,
                "tenant_id": context.tenant_id,
                "request_id": context.request_id,
                "run_id": context.run_id,
                "scope_id": context.scope_id,
                "policy_profile": context.policy_profile,
                "kb_dataset_ids": context.kb_dataset_ids,
            },
        )

        # Use tool-specific timeout if defined (e.g. quiz generation needs 120s)
        from .tools.tool_registry import get_tool_registry
        effective_timeout = context.timeout_ms
        tool_def = get_tool_registry().get_tool(tool_name)
        if tool_def and tool_def.timeout_seconds * 1000 > effective_timeout:
            effective_timeout = tool_def.timeout_seconds * 1000

        # Execute with timeout, retry, and cancellation support
        result = await self._execute_with_retry(
            request=request,
            timeout_ms=effective_timeout,
            max_retries=context.max_retries,
            cancel_event=cancel_event,
        )

        # Record metrics
        duration_ms = (time.time() - start_time) * 1000
        if self.metrics_collector:
            try:
                self.metrics_collector(tool_name, duration_ms, result.success)
            except Exception as e:
                logger.error(f"Failed to record metrics: {e}")

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
                    lambda t: logger.debug(f"Audit write error: {t.exception()}")
                    if not t.cancelled() and t.exception()
                    else None
                )
            except Exception as e:
                logger.debug(f"Audit log failed: {e}")

        # ADR-003 Phase 3: Cache successful results for idempotent tools
        if cache_key and result.success:
            self._cache_put(context.session_id, cache_key, result)

        return result

    async def _execute_with_retry(
        self,
        request: ToolCallRequest,
        timeout_ms: int,
        max_retries: int,
        cancel_event: asyncio.Event | None = None,
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

        for attempt in range(max_retries + 1):
            # Check cancellation before each attempt
            if cancel_event and cancel_event.is_set():
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="Cancelled during execution",
                )

            try:
                # Create execution task
                execution_task = asyncio.create_task(self.tool_registry.execute(request))

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

                # Don't retry on non-transient errors
                if not result.success:
                    error_lower = (result.error or "").lower()
                    # Non-retryable errors
                    if any(
                        phrase in error_lower
                        for phrase in [
                            "unknown tool",
                            "validation error",
                            "missing required",
                            "permission denied",
                            "cancelled",
                        ]
                    ):
                        return result

                return result

            except asyncio.TimeoutError:
                last_error = f"Tool execution timed out after {timeout_ms}ms"
                logger.warning(
                    f"Timeout on attempt {attempt + 1}/{max_retries + 1}: tool={request.tool_name}"
                )

            except asyncio.CancelledError:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="Task cancelled",
                )

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"Error on attempt {attempt + 1}/{max_retries + 1}: "
                    f"tool={request.tool_name} error={e}"
                )

            # Wait before retry (exponential backoff), but check cancellation
            if attempt < max_retries:
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
            error=f"Failed after {max_retries + 1} attempts: {last_error}",
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
        for item in completed:
            if isinstance(item, Exception):
                # Should not happen with return_exceptions=True
                logger.error(f"Unexpected exception in batch: {item}")
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
        results = []
        for req in requests:
            result = await self.invoke(
                tool_name=req["tool_name"],
                arguments=req.get("arguments", {}),
                context=context,
            )
            results.append(result)
        return results

    def get_available_tools(
        self,
        context: ToolInvocationContext,
    ) -> list[str]:
        """Get list of tool names available for this context."""
        tools = self.tool_registry.list_tools(user=context.user)
        return [t.name for t in tools]

    def get_tool_definitions(
        self,
        context: ToolInvocationContext,
        tool_names: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """Get tool definitions for schema generation."""
        tools = self.tool_registry.list_tools(user=context.user)
        if tool_names:
            tools = [t for t in tools if t.name in tool_names]
        return tools

    async def get_tool_definitions_filtered(
        self,
        context: ToolInvocationContext,
        tool_names: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """Get tool definitions with per-tenant policy + MCP filtering (async).

        Falls back to `get_tool_definitions()` if no policies are configured.
        """
        tools = self.get_tool_definitions(context, tool_names)

        # Apply tenant tool policy (whitelist/blacklist/category)
        if self.tenant_tool_policy and context.tenant_id:
            try:
                policy = await self.tenant_tool_policy.get_policy(context.tenant_id)
                tools = self.tenant_tool_policy.filter_tools(tools, policy)
            except Exception as e:
                logger.warning(f"Tenant tool policy filter failed: {e}")

        # Apply tenant MCP config (per-tenant MCP server access)
        if self.tenant_mcp_config and context.tenant_id:
            try:
                mcp_config = await self.tenant_mcp_config.get_config(context.tenant_id)
                tools = self.tenant_mcp_config.filter_mcp_tools(tools, mcp_config)
            except Exception as e:
                logger.warning(f"Tenant MCP config filter failed: {e}")

        return tools


# =============================================================================
# Factory Functions
# =============================================================================


def create_tool_invoker(
    tool_registry: ToolRegistry | None = None,
    rate_limiter: Callable[[str, str], bool] | None = None,
    metrics_collector: Callable[[str, float, bool], None] | None = None,
    tenant_tool_policy: Any | None = None,
    tenant_mcp_config: Any | None = None,
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

    return RegistryToolInvoker(
        tool_registry=registry,
        rate_limiter=rate_limiter,
        metrics_collector=metrics_collector,
        tenant_tool_policy=tenant_tool_policy,
        tenant_mcp_config=tenant_mcp_config,
        tool_audit=tool_audit,
    )


# Convenience alias
get_tool_invoker = create_tool_invoker
