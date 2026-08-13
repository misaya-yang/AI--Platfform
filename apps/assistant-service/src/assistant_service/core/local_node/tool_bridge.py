"""Expose trusted Local Node capabilities through the canonical tool runtime.

This bridge is intentionally request scoped.  It never registers Local Node
tools globally and never dispatches over a user-authenticated HTTP action
endpoint.  Catalog visibility uses one trusted capability snapshot; every
execution resolves device health and grants again before handing a signed
Gateway envelope to the injected provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ai_gateway_core.logging import get_logger, record_internal_exception

from ..tools.tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolRiskLevel,
)
from .gateway_receipt import (
    LocalNodeGatewayReceipt,
    verify_local_node_gateway_receipt,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LocalNodeRunScope:
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    model_provider: str = ""
    model_id: str = ""
    selected_device_id: str = ""
    selected_grant_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocalNodeCapabilitySnapshot:
    """Trusted, effective intersection returned by a Local Node control plane."""

    scope: LocalNodeRunScope
    device_id: str
    lease_id: str
    grant_revision: str
    capabilities: frozenset[str]
    expires_at_ms: int
    trusted_device: bool = False
    healthy: bool = False
    model_data_egress_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capabilities",
            frozenset(str(value) for value in self.capabilities),
        )

    @property
    def binding_sha256(self) -> str:
        payload = {
            "scope": {
                "tenant_id": self.scope.tenant_id,
                "user_id": self.scope.user_id,
                "session_id": self.scope.session_id,
                "run_id": self.scope.run_id,
                "model_provider": self.scope.model_provider,
                "model_id": self.scope.model_id,
                "selected_device_id": self.scope.selected_device_id,
                "selected_grant_ids": list(self.scope.selected_grant_ids),
            },
            "device_id": self.device_id,
            "lease_id": self.lease_id,
            "grant_revision": self.grant_revision,
            "capabilities": sorted(self.capabilities),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalNodeDispatchEnvelope:
    """Provider-facing canonical action envelope; never constructed by the Web client."""

    scope: LocalNodeRunScope
    device_id: str
    lease_id: str
    grant_revision: str
    # Canonical authorization ceiling, signed node authority, and selected
    # operation are deliberately separate.  For example, a signed file.write
    # authority may select the derived file.rollback operation without exposing
    # file.rollback as a separately grantable SaaS capability.
    required_capabilities: frozenset[str]
    action_capability: str
    action_operation: str
    tool_name: str
    arguments: dict[str, Any]
    gateway_receipt: LocalNodeGatewayReceipt


@runtime_checkable
class LocalNodeToolProvider(Protocol):
    """Trusted provider seam supplied by the server composition root.

    ``resolve_capabilities`` must return the effective intersection of paired
    device identity, current grants, node health, lease, and model-data egress
    policy. ``dispatch`` must enforce resource-level grants again and translate
    the Gateway receipt into the paired node's authenticated transport.
    """

    async def resolve_capabilities(
        self,
        scope: LocalNodeRunScope,
    ) -> LocalNodeCapabilitySnapshot | None: ...

    async def supports_capability_set(
        self,
        scope: LocalNodeRunScope,
        capabilities: frozenset[str],
    ) -> bool: ...

    async def dispatch(self, envelope: LocalNodeDispatchEnvelope) -> ToolCallResult: ...


@dataclass(frozen=True, slots=True)
class _LocalToolSpec:
    name: str
    required_capabilities: frozenset[str]
    action_capability: str
    action_operation: str
    description: str
    argument_schema: dict[str, Any]
    operation_kind: str
    risk_level: ToolRiskLevel
    requires_confirmation: bool
    timeout_seconds: int
    relevance_keywords: tuple[str, ...]
    idempotency_supported: bool = False
    read_back_available: bool = False
    compensation_available: bool = False
    compensation_tool: str | None = None
    compensates_tool: str | None = None
    evidence_contract: dict[str, Any] | None = None


_RELATIVE_PATH = {
    "type": "string",
    "description": "Path relative to an explicitly granted directory; absolute paths are invalid.",
    "minLength": 1,
    "maxLength": 2048,
}
_GRANT_ID = {
    "type": "string",
    "description": "Opaque directory or application grant identifier shown by the Local Node.",
    "minLength": 1,
    "maxLength": 200,
}


def _object_schema(
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_TOOL_SPECS = (
    _LocalToolSpec(
        name="local_file_list",
        required_capabilities=frozenset({"file.list"}),
        action_capability="file.list",
        action_operation="file.list",
        description=(
            "List entries inside one explicitly granted local directory. Results use only "
            "grant-relative paths and include kind, size, and modification metadata."
        ),
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["grant_id", "path"],
        ),
        operation_kind="read",
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=30,
        relevance_keywords=("local", "file", "folder", "directory", "list"),
        evidence_contract={
            "path_scope": "grant_relative_only",
            "fields": ["relative_path", "kind", "size", "modified_ns"],
            "content_included": False,
        },
    ),
    _LocalToolSpec(
        name="local_file_read",
        required_capabilities=frozenset({"file.read"}),
        action_capability="file.read",
        action_operation="file.read",
        description=(
            "Read bounded content from a file inside one explicitly granted local directory. "
            "Results include the relative path, encoding, exact byte size, and SHA-256."
        ),
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1_048_576},
            },
            ["grant_id", "path"],
        ),
        operation_kind="read",
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=30,
        relevance_keywords=("local", "file", "read", "open", "content"),
        evidence_contract={
            "path_scope": "grant_relative_only",
            "fields": [
                "relative_path",
                "content",
                "encoding",
                "size",
                "sha256",
            ],
            "hash": "sha256_exact_bytes",
        },
    ),
    _LocalToolSpec(
        name="local_file_hash",
        required_capabilities=frozenset({"file.read"}),
        action_capability="file.read",
        action_operation="file.hash",
        description=(
            "Hash one bounded file inside an explicitly granted local directory without "
            "returning its content. Results include the relative path, encoding, exact byte "
            "size, and SHA-256."
        ),
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8_388_608},
            },
            ["grant_id", "path"],
        ),
        operation_kind="read",
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=30,
        relevance_keywords=("local", "file", "hash", "sha256", "integrity"),
        evidence_contract={
            "path_scope": "grant_relative_only",
            "fields": ["relative_path", "encoding", "size", "sha256"],
            "content_included": False,
            "hash": "sha256_exact_bytes",
        },
    ),
    _LocalToolSpec(
        name="local_file_search",
        required_capabilities=frozenset({"file.search"}),
        action_capability="file.search",
        action_operation="file.search",
        description=(
            "Search bounded text within one explicitly granted local directory. Matches "
            "include relative path, 1-based line and column, preview, and file SHA-256."
        ),
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["grant_id", "path", "query"],
        ),
        operation_kind="read",
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=45,
        relevance_keywords=("local", "file", "search", "grep", "find"),
        evidence_contract={
            "path_scope": "grant_relative_only",
            "fields": ["relative_path", "line", "column", "preview", "file_sha256"],
            "line_numbering": "one_based",
            "hash": "sha256_exact_bytes",
        },
    ),
    _LocalToolSpec(
        name="local_file_watch",
        required_capabilities=frozenset({"file.watch"}),
        action_capability="file.watch",
        action_operation="file.watch",
        description=(
            "Observe bounded metadata changes under one explicitly granted local directory. "
            "Watch events never include file content."
        ),
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "after_revision": {"type": "string", "maxLength": 200},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30_000},
            },
            ["grant_id", "path"],
        ),
        operation_kind="read",
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=35,
        relevance_keywords=("local", "file", "watch", "change", "realtime"),
        evidence_contract={
            "path_scope": "grant_relative_only",
            "fields": [
                "sequence",
                "kind",
                "relative_path",
                "previous_path",
                "sha256",
                "size",
                "observed_at",
            ],
            "content_included": False,
            "metadata_only": True,
        },
    ),
    _LocalToolSpec(
        name="local_file_write",
        required_capabilities=frozenset({"file.write"}),
        action_capability="file.write",
        action_operation="file.write",
        description="Atomically write a file inside one explicitly granted local directory.",
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "content": {"type": "string", "maxLength": 1_048_576},
                "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            ["grant_id", "path", "content"],
        ),
        operation_kind="write",
        risk_level=ToolRiskLevel.MEDIUM,
        requires_confirmation=True,
        timeout_seconds=45,
        relevance_keywords=("local", "file", "write", "edit", "save"),
        compensation_available=True,
        compensation_tool="local_file_rollback",
    ),
    _LocalToolSpec(
        name="local_file_rollback",
        required_capabilities=frozenset({"file.write"}),
        action_capability="file.write",
        action_operation="file.rollback",
        description=(
            "Restore one local file from an opaque rollback receipt, only if its current "
            "content still matches the expected SHA-256."
        ),
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "path": _RELATIVE_PATH,
                "rollback_ref": {
                    "type": "string",
                    "description": "Opaque rollback reference returned by local_file_write.",
                    "pattern": "^rollback_[A-Za-z0-9_-]{8,200}$",
                    "maxLength": 220,
                },
                "expected_current_sha256": {
                    "type": "string",
                    "description": (
                        "Exact SHA-256 observed after the write; rollback stops if the file "
                        "changed since that receipt."
                    ),
                    "pattern": "^[a-f0-9]{64}$",
                },
            },
            ["grant_id", "path", "rollback_ref", "expected_current_sha256"],
        ),
        operation_kind="write",
        risk_level=ToolRiskLevel.MEDIUM,
        requires_confirmation=True,
        timeout_seconds=45,
        relevance_keywords=("local", "file", "rollback", "restore", "undo"),
        compensates_tool="local_file_write",
    ),
    _LocalToolSpec(
        name="local_process_run",
        required_capabilities=frozenset({"process.run"}),
        action_capability="process.run",
        action_operation="process.run",
        description="Run an allowlisted local program with structured argv and a minimal environment.",
        argument_schema=_object_schema(
            {
                "grant_id": _GRANT_ID,
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 4096},
                    "minItems": 1,
                    "maxItems": 128,
                },
                "cwd": _RELATIVE_PATH,
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120_000},
                "network_policy": {"type": "string", "enum": ["deny", "allow_granted_domains"]},
            },
            ["grant_id", "argv", "cwd", "network_policy"],
        ),
        operation_kind="write",
        risk_level=ToolRiskLevel.HIGH,
        requires_confirmation=True,
        timeout_seconds=125,
        relevance_keywords=("local", "process", "command", "run", "shell"),
    ),
    _LocalToolSpec(
        name="local_screen_observe",
        required_capabilities=frozenset({"app.observe", "screen.observe"}),
        action_capability="screen.observe",
        action_operation="screen.observe",
        description="Observe one explicitly granted local application window.",
        argument_schema=_object_schema(
            {
                "app_grant_id": _GRANT_ID,
                "window_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "include_screenshot": {"type": "boolean"},
                "provider_safety_checks": {
                    "type": "array",
                    "description": (
                        "Provider-originated warnings that can only require additional "
                        "platform approval; they never authorize this observation."
                    ),
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 1024},
                            "code": {"type": "string", "minLength": 1, "maxLength": 1024},
                            "message": {"type": "string", "minLength": 1, "maxLength": 8192},
                        },
                        "required": ["id", "code", "message"],
                        "additionalProperties": False,
                    },
                },
            },
            ["app_grant_id", "window_id", "include_screenshot"],
        ),
        operation_kind="read",
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=30,
        relevance_keywords=("local", "computer", "app", "window", "screenshot", "observe"),
    ),
    _LocalToolSpec(
        name="local_app_control",
        required_capabilities=frozenset({"app.control"}),
        action_capability="app.control",
        action_operation="app.control",
        description="Perform a bounded action batch in one freshly observed, explicitly granted application window.",
        argument_schema=_object_schema(
            {
                "app_grant_id": _GRANT_ID,
                "window_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "observation_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "click",
                                    "double_click",
                                    "scroll",
                                    "type",
                                    "keypress",
                                    "drag",
                                    "wait",
                                ],
                            },
                            "x": {"type": "integer", "minimum": 0},
                            "y": {"type": "integer", "minimum": 0},
                            "text": {"type": "string", "maxLength": 10_000},
                            "key": {"type": "string", "maxLength": 100},
                            "scroll_y": {"type": "integer", "minimum": -10_000, "maximum": 10_000},
                            "duration_ms": {"type": "integer", "minimum": 1, "maximum": 10_000},
                            "from_x": {"type": "integer", "minimum": 0},
                            "from_y": {"type": "integer", "minimum": 0},
                            "to_x": {"type": "integer", "minimum": 0},
                            "to_y": {"type": "integer", "minimum": 0},
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                },
                "provider_safety_checks": {
                    "type": "array",
                    "description": (
                        "Provider-originated warnings bound into the exact Gateway approval; "
                        "they can only increase restrictions."
                    ),
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 1024},
                            "code": {"type": "string", "minLength": 1, "maxLength": 1024},
                            "message": {"type": "string", "minLength": 1, "maxLength": 8192},
                        },
                        "required": ["id", "code", "message"],
                        "additionalProperties": False,
                    },
                },
            },
            ["app_grant_id", "window_id", "observation_id", "actions"],
        ),
        operation_kind="write",
        risk_level=ToolRiskLevel.HIGH,
        requires_confirmation=True,
        timeout_seconds=60,
        relevance_keywords=("local", "computer", "app", "click", "type", "scroll", "control"),
    ),
)


def _snapshot_is_effective(
    snapshot: Any,
    scope: LocalNodeRunScope,
) -> bool:
    return bool(
        isinstance(snapshot, LocalNodeCapabilitySnapshot)
        and snapshot.scope == scope
        and snapshot.device_id
        and snapshot.lease_id
        and snapshot.grant_revision
        and snapshot.expires_at_ms >= int(time.time() * 1000)
        and snapshot.trusted_device
        and snapshot.healthy
        and snapshot.model_data_egress_allowed
    )


class _LocalNodeExecutor(ToolExecutor):
    def __init__(
        self,
        *,
        provider: LocalNodeToolProvider,
        scope: LocalNodeRunScope,
        catalog_snapshot: LocalNodeCapabilitySnapshot,
        spec: _LocalToolSpec,
    ) -> None:
        self._provider = provider
        self._scope = scope
        self._catalog_snapshot = catalog_snapshot
        self._spec = spec

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        metadata = request.metadata or {}
        if any(
            str(metadata.get(name) or "") != expected
            for name, expected in (
                ("tenant_id", self._scope.tenant_id),
                ("user_id", self._scope.user_id),
                ("session_id", self._scope.session_id),
                ("run_id", self._scope.run_id),
            )
        ):
            return self._denied(request, "LOCAL_NODE_SCOPE_MISMATCH")

        gateway_receipt = metadata.get("_local_node_gateway_receipt")
        if not isinstance(
            gateway_receipt, LocalNodeGatewayReceipt
        ) or not verify_local_node_gateway_receipt(
            gateway_receipt,
            tenant_id=self._scope.tenant_id,
            user_id=self._scope.user_id,
            session_id=self._scope.session_id,
            run_id=self._scope.run_id,
            tool_name=self._spec.name,
            arguments=request.arguments,
            device_id=self._catalog_snapshot.device_id,
            lease_id=self._catalog_snapshot.lease_id,
            grant_revision=self._catalog_snapshot.grant_revision,
            binding_sha256=self._catalog_snapshot.binding_sha256,
        ):
            return self._denied(request, "LOCAL_NODE_GATEWAY_RECEIPT_REQUIRED")
        if self._spec.operation_kind != "read" and not gateway_receipt.approval_consumed:
            return self._denied(request, "LOCAL_NODE_TRUSTED_APPROVAL_REQUIRED")

        try:
            fresh = await self._provider.resolve_capabilities(self._scope)
        except Exception as exc:  # noqa: BLE001 - authorization outages fail closed
            record_internal_exception(
                __name__, "assistant.core.local_node.tool_bridge.internal_failure", exc
            )
            return self._denied(request, "LOCAL_NODE_AUTHORIZATION_UNAVAILABLE")
        if not _snapshot_is_effective(fresh, self._scope):
            return self._denied(request, "LOCAL_NODE_UNAVAILABLE")
        assert isinstance(fresh, LocalNodeCapabilitySnapshot)
        required_capabilities = set(self._spec.required_capabilities)
        if (
            self._spec.name == "local_screen_observe"
            and request.arguments.get("include_screenshot") is True
        ):
            required_capabilities.add("screen.share")
        if (
            not required_capabilities.issubset(fresh.capabilities)
            or fresh.device_id != self._catalog_snapshot.device_id
            or fresh.lease_id != self._catalog_snapshot.lease_id
            or fresh.grant_revision != self._catalog_snapshot.grant_revision
            or fresh.binding_sha256 != self._catalog_snapshot.binding_sha256
        ):
            return self._denied(request, "LOCAL_NODE_CAPABILITY_REVOKED")

        envelope = LocalNodeDispatchEnvelope(
            scope=self._scope,
            device_id=fresh.device_id,
            lease_id=fresh.lease_id,
            grant_revision=fresh.grant_revision,
            required_capabilities=frozenset(required_capabilities),
            action_capability=self._spec.action_capability,
            action_operation=self._spec.action_operation,
            tool_name=self._spec.name,
            arguments=copy.deepcopy(request.arguments),
            gateway_receipt=gateway_receipt,
        )
        try:
            result = await self._provider.dispatch(envelope)
        except Exception as exc:  # noqa: BLE001 - a write transport failure has unknown outcome
            record_internal_exception(
                __name__, "assistant.core.local_node.tool_bridge.internal_failure", exc
            )
            if self._spec.operation_kind != "read":
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="SIDE_EFFECT_UNKNOWN",
                    metadata={
                        "side_effect_unknown": True,
                        "side_effect_state": "unknown",
                        "blind_replay_allowed": False,
                        "execution_surface": "local_node",
                    },
                )
            return self._denied(request, "LOCAL_NODE_DISPATCH_UNAVAILABLE")
        if not isinstance(result, ToolCallResult):
            return self._denied(request, "LOCAL_NODE_INVALID_RECEIPT")
        result.call_id = request.call_id
        result.tool_name = request.tool_name
        result.metadata = {
            **dict(result.metadata or {}),
            "execution_surface": "local_node",
            "device_id": fresh.device_id,
            "grant_revision": fresh.grant_revision,
            "gateway_receipt_id": gateway_receipt.receipt_id,
        }
        return result

    @staticmethod
    def _denied(request: ToolCallRequest, code: str) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=False,
            error=code,
            metadata={
                "execution_surface": "local_node",
                "execution_authorized": False,
                "side_effect_state": "not_started",
            },
        )


def _definition(
    spec: _LocalToolSpec,
    snapshot: LocalNodeCapabilitySnapshot,
) -> ToolDefinition:
    return ToolDefinition(
        name=spec.name,
        description=spec.description,
        parameters=[],
        argument_schema=copy.deepcopy(spec.argument_schema),
        category=ToolCategory.LOCAL,
        risk_level=spec.risk_level,
        requires_confirmation=spec.requires_confirmation,
        relevance_keywords=list(spec.relevance_keywords),
        timeout_seconds=spec.timeout_seconds,
        max_retries=0,
        sandbox_profile="local_node",
        capability_metadata={
            "execution_surface": "local_node",
            "requires_gateway": True,
            "local_node_capabilities": sorted(spec.required_capabilities),
            "local_node_action_capability": spec.action_capability,
            "local_node_action_operation": spec.action_operation,
            "local_node_device_id": snapshot.device_id,
            "local_node_lease_id": snapshot.lease_id,
            "local_node_grant_revision": snapshot.grant_revision,
            "local_node_binding_sha256": snapshot.binding_sha256,
            "operation_kind": spec.operation_kind,
            "read_only": spec.operation_kind == "read",
            "external_service": True,
            "idempotency_supported": spec.idempotency_supported,
            "read_back_available": (spec.operation_kind == "read" or spec.read_back_available),
            "compensation_available": spec.compensation_available,
            **({"compensation_tool": spec.compensation_tool} if spec.compensation_tool else {}),
            **({"compensates_tool": spec.compensates_tool} if spec.compensates_tool else {}),
            **(
                {"evidence_contract": copy.deepcopy(spec.evidence_contract)}
                if spec.evidence_contract is not None
                else {}
            ),
        },
        audit_shape={
            "input": "redacted_summary",
            "output": (
                "typed_grant_relative_evidence"
                if spec.evidence_contract is not None
                else "receipt_only"
            ),
        },
        redaction_policy="strict",
    )


async def prepare_local_node_runtime_tools(
    ctx: Any,
    provider: LocalNodeToolProvider | None,
    *,
    model_provider: str | None = None,
    model_id: str | None = None,
) -> int:
    """Merge eligible Local Node tools into this run's isolated registry.

    Provider absence, OS-Agent disablement, resolver errors, empty grants,
    untrusted/offline devices, expired leases, or denied model-data egress all
    produce exactly zero Local Node tools.
    """

    if provider is None or not bool(getattr(ctx.config, "os_agent_enabled", False)):
        return 0
    scope = LocalNodeRunScope(
        tenant_id=str(ctx.tenant_id or ""),
        user_id=str(ctx.user_id or ""),
        session_id=str(ctx.session_id or ""),
        run_id=str(ctx.run_id or ""),
        model_provider=str(
            model_provider if model_provider is not None else getattr(ctx, "model_provider", "")
        ),
        model_id=str(model_id if model_id is not None else getattr(ctx, "model_id", "")),
        selected_device_id=str(getattr(ctx.config, "local_node_device_id", "") or ""),
        selected_grant_ids=tuple(
            str(value) for value in (getattr(ctx.config, "local_node_grant_ids", ()) or ())
        ),
    )
    if any(not value for value in (scope.tenant_id, scope.user_id, scope.session_id, scope.run_id)):
        return 0
    try:
        snapshot = await provider.resolve_capabilities(scope)
    except Exception as exc:  # noqa: BLE001 - resolver outages hide the entire surface
        record_internal_exception(
            __name__, "assistant.core.local_node.tool_bridge.internal_failure", exc
        )
        logger.warning("Local Node capability resolution failed; tools remain unavailable")
        return 0
    if not _snapshot_is_effective(snapshot, scope):
        return 0
    assert isinstance(snapshot, LocalNodeCapabilitySnapshot)
    selected_specs: list[_LocalToolSpec] = []
    for spec in _TOOL_SPECS:
        if not spec.required_capabilities.issubset(snapshot.capabilities):
            continue
        try:
            supported = await provider.supports_capability_set(
                scope,
                spec.required_capabilities,
            )
        except Exception as exc:  # noqa: BLE001 - compound-grant ambiguity hides the tool
            record_internal_exception(
                __name__, "assistant.core.local_node.tool_bridge.internal_failure", exc
            )
            supported = False
        if supported is True:
            selected_specs.append(spec)
    if not selected_specs:
        return 0

    from ..tools.tool_registry import ToolRegistry

    runtime_registry = ctx.runtime_tool_registry
    if runtime_registry is None:
        runtime_registry = ToolRegistry()
    existing_names = {item.name for item in runtime_registry.list_tools(user=ctx.user)}
    if any(spec.name in existing_names for spec in selected_specs):
        logger.warning("Local Node tool name collision; tools remain unavailable")
        return 0

    for spec in selected_specs:
        runtime_registry.register(
            _definition(spec, snapshot),
            _LocalNodeExecutor(
                provider=provider,
                scope=scope,
                catalog_snapshot=snapshot,
                spec=spec,
            ),
        )
    ctx.runtime_tool_registry = runtime_registry
    return len(selected_specs)
