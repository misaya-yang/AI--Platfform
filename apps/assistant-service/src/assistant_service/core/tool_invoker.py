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
import hashlib
import json as _json
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger, record_internal_exception
from ai_gateway_core.security import redact_trace_text

from .tool_invocation_contracts import (
    BatchInvocationResult,
    CapabilityAllowlist,
    ToolExecutionPolicy,
    ToolInvocationContext,
    ToolInvoker,
    ToolPolicySnapshot,
)

if TYPE_CHECKING:
    from .tools.tool_registry import ToolCallRequest, ToolCallResult, ToolDefinition, ToolRegistry

logger = get_logger(__name__)

__all__ = [
    "BatchInvocationResult",
    "CapabilityAllowlist",
    "RegistryToolInvoker",
    "ToolExecutionPolicy",
    "ToolInvocationContext",
    "ToolInvoker",
    "ToolPolicySnapshot",
    "create_tool_invoker",
]

_MAX_PUBLIC_ERROR_CHARS = 200
_TOOL_CANCEL_GRACE_SECONDS = 0.1


def _consume_task_outcome(task: asyncio.Task[Any]) -> None:
    """Consume a detached task outcome so bounded cancellation stays warning-free."""

    if task.cancelled():
        return
    try:
        task.exception()
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tool_invoker.cancelled_child_failure", exc
        )


async def _cancel_task_bounded(task: asyncio.Task[Any] | None) -> None:
    """Request cancellation without letting a non-cooperative tool block its caller."""

    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_TOOL_CANCEL_GRACE_SECONDS,
        )
    except (TimeoutError, asyncio.CancelledError):
        if not task.done():
            task.add_done_callback(_consume_task_outcome)
    except Exception as exc:
        # The child failed while responding to cancellation.  Its exception is
        # intentionally not allowed to replace the caller's cancellation or
        # timeout outcome.
        record_internal_exception(
            __name__, "assistant.core.tool_invoker.cancelled_child_failure", exc
        )
        return


def _safe_public_error(value: Any) -> str:
    """Return a bounded, shared-redaction error while preserving safe short messages."""

    try:
        text = str(value) if isinstance(value, BaseException) else value
        return redact_trace_text(text, limit=_MAX_PUBLIC_ERROR_CHARS)
    except Exception as exc:
        record_internal_exception(__name__, "assistant.core.tool_invoker.internal_failure", exc)
        return "Tool execution failed"


def _tool_log_label(tool_name: Any) -> str:
    """Return a stable non-reversible label for untrusted tool names."""

    try:
        digest = hashlib.sha256(str(tool_name).encode("utf-8", errors="replace")).hexdigest()
    except Exception as exc:
        record_internal_exception(__name__, "assistant.core.tool_invoker.internal_failure", exc)
        return "tool_sha256=unavailable"
    return f"tool_sha256={digest[:16]}"


def _log_audit_task_completion(task: asyncio.Task[Any], tool_label: str) -> None:
    """Retrieve an async audit failure without exposing its exception payload."""

    if task.cancelled():
        return
    try:
        error = task.exception()
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tool_invoker.internal_failure", exc
        )
        return
    if error is not None:
        record_internal_exception(
            __name__,
            "assistant.tool_audit.background_task_failed",
            error,
            level=logging.DEBUG,
        )


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
        self._tool_discovery_gateway: Any | None = None

        # ADR-003 Phase 3: Principal-and-scope isolated tool result cache.
        # Key: (tenant_id, user_id, agent_scope/session_id, cache_key).
        self._result_cache: dict[tuple[str, str, str, str], tuple[Any, float]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._cache_max_size = 200
        # Only idempotent tools are cacheable
        self._cacheable_prefixes = ("search_knowledge_base",)

    def configure_tool_discovery_gateway(self, gateway: Any | None) -> None:
        """Bind the canonical gateway used for discovered underlying calls."""

        self._tool_discovery_gateway = gateway

    async def _invoke_discovered_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
    ) -> Any:
        gateway = self._tool_discovery_gateway
        if gateway is None:
            from .tools.tool_registry import ToolCallResult

            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="TOOL_DISCOVERY_CALL_UNAVAILABLE",
            )
        # Capability-plane discovery is a controlled escape hatch from the
        # bridge to one exact authorized tool.  Preserve the normal Gateway
        # approval boundary for write/unknown targets; otherwise a read-only
        # ``tool_call`` bridge could accidentally dispatch a mutating target
        # before the Gateway sees it.
        forwarded_arguments = arguments
        if (context.metadata or {}).get("capability_plane"):
            runtime_definition = (
                context.runtime_tool_registry.get_tool(tool_name)
                if context.runtime_tool_registry is not None
                else None
            )
            definition = runtime_definition or self.tool_registry.get_tool(tool_name)
            binding_type = ""
            if context.capability_allowlist is not None:
                binding = context.capability_allowlist.binding(tool_name) or {}
                binding_type = str(binding.get("type") or "")
            from .tools.tool_registry import tool_operation_kind

            if tool_operation_kind(definition, binding_type=binding_type) != "read":
                forwarded_arguments = {
                    **arguments,
                    "_middleware_approval_required": True,
                }
        return await gateway.invoke_tool(
            tool_name=tool_name,
            arguments=forwarded_arguments,
            context=context,
        )

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
                record_internal_exception(
                    __name__,
                    f"assistant.tool_policy.catalog_failed.{_tool_log_label('catalog')}",
                    exc,
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
                record_internal_exception(
                    __name__,
                    f"assistant.mcp_policy.catalog_failed.{_tool_log_label('mcp_catalog')}",
                    exc,
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
                record_internal_exception(
                    __name__,
                    f"assistant.tool_audit.denied_failed.{_tool_log_label(tool_name)}",
                    exc,
                )

        return ToolCallResult(
            call_id=call_id,
            tool_name=tool_name,
            success=False,
            error=safe_error,
            duration_ms=duration_ms,
            metadata=dict(metadata or {}),
        )

    def _cached_tool_result(
        self,
        *,
        call_id: str,
        cached: Any,
        policy_metadata: dict[str, Any],
        validation_receipt: dict[str, Any] | None,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        tool_label: str,
    ) -> Any:
        """Return an authorized cache hit and emit its best-effort audit."""

        from .tools.tool_registry import ToolCallResult

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
                task = asyncio.create_task(self.tool_audit.log(entry))
                task.add_done_callback(
                    lambda finished: _log_audit_task_completion(finished, tool_label)
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.tool_invoker.internal_failure", exc
                )
        return cached_copy

    def _finalize_tool_result(
        self,
        *,
        result: Any,
        start_time: float,
        tool_name: str,
        tool_label: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cache_key: str | None,
        cache_scope: tuple[str, str, str],
        policy_metadata: dict[str, Any],
        validation_receipt: dict[str, Any] | None,
    ) -> Any:
        """Apply metrics, audit, caching and public metadata to one result."""

        if result.error is not None:
            result.error = _safe_public_error(result.error)

        duration_ms = (time.time() - start_time) * 1000
        if self.metrics_collector:
            try:
                self.metrics_collector(tool_name, duration_ms, result.success)
            except Exception as exc:
                record_internal_exception(
                    __name__, f"assistant.tool_metrics.failed.{tool_label}", exc
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
                    output_status="success" if result.success else "error",
                    error_message=result.error if not result.success else None,
                    latency_ms=duration_ms,
                )
                task = asyncio.create_task(self.tool_audit.log(entry))
                task.add_done_callback(
                    lambda finished: _log_audit_task_completion(finished, tool_label)
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, f"assistant.tool_audit.schedule_failed.{tool_label}", exc
                )

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

    def _tool_discovery_metadata(
        self,
        *,
        tool_name: str,
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        """Return process-local bridge callbacks without trace serialization."""

        from .tools.tool_discovery import is_tool_discovery_bridge

        if not is_tool_discovery_bridge(tool_name):
            return {}
        return {
            "_tool_discovery_context": context,
            "_tool_discovery_catalog_provider": self.get_tool_definitions_filtered,
            "_tool_discovery_caller": self._invoke_discovered_tool,
        }

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolCallResult:
        """Invoke one tool through scoped authorization, policy and execution."""
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
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.tool_invoker.internal_failure", exc
                )
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
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.tool_invoker.internal_failure", exc
                )
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
            return self._cached_tool_result(
                call_id=call_id,
                cached=cached,
                policy_metadata=policy_metadata,
                validation_receipt=validation_receipt,
                context=context,
                tool_name=tool_name,
                arguments=arguments,
                tool_label=tool_label,
            )

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

        # Build request. Discovery callbacks remain process-local and are
        # consumed by the bridge executor before public result projection.
        private_discovery_metadata = self._tool_discovery_metadata(
            tool_name=tool_name,
            context=context,
        )

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
                **private_discovery_metadata,
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
        if tool_definition is not None:
            resolved_timeout = execution_registry.effective_execution_timeout(
                request,
                tool_definition,
            )
            if isinstance(resolved_timeout, (int, float)) and not isinstance(
                resolved_timeout,
                bool,
            ):
                tool_timeout_seconds = resolved_timeout
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
                            # Shield the child so caller cancellation returns
                            # control here immediately; direct awaiting lets a
                            # cancellation-suppressing MCP client hold the
                            # parent task forever before our grace timer runs.
                            result = await asyncio.shield(mcp_task)
                        else:
                            cancel_task = asyncio.create_task(cancel_event.wait())
                            done, _pending = await asyncio.wait(
                                {mcp_task, cancel_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if mcp_task in done:
                                result = mcp_task.result()
                            else:
                                await _cancel_task_bounded(mcp_task)
                                if execution_policy.may_have_external_side_effect:
                                    result = ToolCallResult(
                                        call_id=call_id,
                                        tool_name=tool_name,
                                        success=False,
                                        error="SIDE_EFFECT_UNKNOWN",
                                        metadata=self._side_effect_unknown_metadata(
                                            execution_policy,
                                            cause="cancelled_after_dispatch",
                                        ),
                                    )
                                else:
                                    result = ToolCallResult(
                                        call_id=call_id,
                                        tool_name=tool_name,
                                        success=False,
                                        error="Cancelled during MCP execution",
                                    )
                    except asyncio.CancelledError:
                        await _cancel_task_bounded(mcp_task)
                        raise
                    finally:
                        await _cancel_task_bounded(cancel_task)
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
        except asyncio.CancelledError:
            if operation_claimed:
                # Dispatch may already have crossed the external side-effect
                # boundary.  Preserve a fence even though cancellation must be
                # propagated to the run-level deadline owner.
                context.uncertain_operation_fingerprints.add(fingerprint)
            raise
        finally:
            if operation_claimed:
                context.inflight_operation_fingerprints.discard(fingerprint)

        return self._finalize_tool_result(
            result=result,
            start_time=start_time,
            tool_name=tool_name,
            tool_label=tool_label,
            arguments=arguments,
            context=context,
            cache_key=cache_key,
            cache_scope=cache_scope,
            policy_metadata=policy_metadata,
            validation_receipt=validation_receipt,
        )

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
            cancel_task: asyncio.Task[bool] | None = None
            try:
                # Create execution task
                execution_task = asyncio.create_task(execution_registry.execute(request))

                # If we have a cancel event, race between execution and cancellation
                if cancel_event:
                    cancel_task = asyncio.create_task(cancel_event.wait())

                    done, _pending = await asyncio.wait(
                        [execution_task, cancel_task],
                        timeout=timeout_ms / 1000,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Check if cancellation won the race
                    if cancel_task in done:
                        await _cancel_task_bounded(execution_task)
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
                        await _cancel_task_bounded(execution_task)
                        raise asyncio.TimeoutError()

                    # Get the execution result
                    result = execution_task.result()
                else:
                    # asyncio.wait_for waits for a cancelled coroutine to
                    # finish.  A tool that suppresses CancelledError can thus
                    # defeat both its own timeout and the run wall budget.
                    done, _pending = await asyncio.wait(
                        {execution_task},
                        timeout=timeout_ms / 1000,
                    )
                    if not done:
                        await _cancel_task_bounded(execution_task)
                        raise asyncio.TimeoutError()
                    result = execution_task.result()

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
                await _cancel_task_bounded(execution_task)
                raise

            except Exception as exc:
                record_internal_exception(
                    __name__,
                    f"assistant.tool_retry.failed.{_tool_log_label(request.tool_name)}",
                    exc,
                )
                last_error = _safe_public_error(exc)
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

            finally:
                await _cancel_task_bounded(cancel_task)

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
                record_internal_exception(
                    __name__,
                    f"assistant.tool_batch.failed.{_tool_log_label(tool_name)}",
                    item,
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
                except Exception as exc:
                    record_internal_exception(
                        __name__, "assistant.core.tool_invoker.internal_failure", exc
                    )
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
