"""Concrete, explicitly injected Local Node provider for the canonical tool path.

This module joins the request-scoped ``LocalNodeToolProvider`` contract to the
Local Node control-plane state machine.  It is deliberately absent from the
default composition root: a deployment must inject durable control-plane
state, authenticated device delivery, a deterministic run/device binding, and
a platform action signer.  Missing or ambiguous authority produces no tools.
"""

from __future__ import annotations

import copy
import hashlib
import math
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Protocol, runtime_checkable

from ai_gateway_core.logging import record_internal_exception

from ..tools.tool_registry import ToolCallResult
from .control_plane import (
    LocalNodeControlPlaneService,
    LocalNodeRepository,
    canonical_digest,
    derive_action_id,
)
from .gateway_receipt import verify_local_node_gateway_receipt
from .protocol import LOCAL_NODE_PROTOCOL_VERSION
from .tool_bridge import (
    LocalNodeCapabilitySnapshot,
    LocalNodeDispatchEnvelope,
    LocalNodeRunScope,
    LocalNodeToolProvider,
)


@dataclass(frozen=True, slots=True)
class LocalNodeRunBinding:
    """Trusted server-side choice of one paired device for one Agent run."""

    scope: LocalNodeRunScope
    device_id: str
    lease_id: str
    expires_at_ms: int
    trusted_device: bool
    model_data_egress_allowed: bool = False
    model_provider: str = ""
    model_id: str = ""
    model_egress_purpose: str = ""
    selected_grant_ids: frozenset[str] = frozenset()


@runtime_checkable
class LocalNodeRunBindingResolver(Protocol):
    """Resolve a run to exactly one device without using model/Web input."""

    async def resolve(self, scope: LocalNodeRunScope) -> LocalNodeRunBinding | None: ...


@runtime_checkable
class LocalNodePlatformActionSigner(Protocol):
    """Sign canonical companion ``ActionContext`` bytes with platform authority."""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> str: ...


@dataclass(frozen=True, slots=True)
class LocalNodeTrustedApprovalProof:
    approval_id: str
    action_id: str
    device_id: str
    arguments_digest: str
    target_snapshot_digest: str
    policy_snapshot_digest: str
    nonce: str
    expires_at: float
    local_signature: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "action_id": self.action_id,
            "device_id": self.device_id,
            "arguments_digest": self.arguments_digest,
            "target_snapshot_digest": self.target_snapshot_digest,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
            "local_signature": self.local_signature,
        }


@runtime_checkable
class LocalNodeTrustedApprovalRegistrar(Protocol):
    """Register the already-consumed Gateway approval on the paired node.

    This is a device-authenticated control-channel operation, not a Web/API
    approval.  The resulting trusted-local receipt must flow back through
    ``record_approval_receipt`` before the control plane releases delivery.
    """

    async def request_local_approval(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        approval_id: str,
        signed_action: Mapping[str, Any],
        normalized_arguments: Mapping[str, Any],
    ) -> None: ...


@runtime_checkable
class LocalNodeTrustedApprovalReceiptVerifier(Protocol):
    """Verify the independent device-local approval receipt before release."""

    async def verify(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        receipt: Mapping[str, Any],
    ) -> bool: ...


@runtime_checkable
class LocalNodeActionResultWaiter(Protocol):
    """Await one terminal device result already authenticated by its channel.

    Implementations own the durable action-id correlation and must return only
    a terminal ``succeeded`` result for the exact owner/device/action tuple.
    ``None`` means no trustworthy result arrived before the bounded deadline.
    """

    async def await_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class LocalNodeDeviceChannelPrincipal:
    tenant_id: str
    user_id: str
    device_id: str
    channel_id: str


@dataclass(frozen=True, slots=True)
class _CompanionActionContext:
    """Exact server mirror of the companion's signed ActionContext v1 wire."""

    action_id: str
    idempotency_key: str
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    agent_id: str
    agent_version: str
    call_id: str
    device_id: str
    envelope_version: int
    capability: str
    tool_name: str
    operation: str
    capability_lease_id: str
    resource_refs: tuple[str, ...]
    arguments_digest: str
    target_snapshot_digest: str
    policy_snapshot_digest: str
    nonce: str
    issued_at: float
    expires_at: float
    platform_key_id: str
    platform_signature: str
    approval: LocalNodeTrustedApprovalProof | None = None
    trace_context: Mapping[str, str] | None = None

    @classmethod
    def create(
        cls,
        *,
        normalized_arguments: Mapping[str, Any],
        ttl_seconds: int,
        **values: Any,
    ) -> _CompanionActionContext:
        issued_at = time.time()
        return cls(
            **values,
            arguments_digest=_plain_digest(normalized_arguments),
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            platform_signature="",
        )

    def canonical_signed_payload(self) -> bytes:
        payload = {
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "call_id": self.call_id,
            "device_id": self.device_id,
            "envelope_version": self.envelope_version,
            "capability": self.capability,
            "tool_name": self.tool_name,
            "operation": self.operation,
            "capability_lease_id": self.capability_lease_id,
            "resource_refs": list(self.resource_refs),
            "resource_refs_digest": _plain_digest(list(self.resource_refs)),
            "arguments_digest": self.arguments_digest,
            "target_snapshot_digest": self.target_snapshot_digest,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "platform_key_id": self.platform_key_id,
            "approval": None if self.approval is None else self.approval.to_wire(),
        }
        import json

        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _companion_action_to_wire(action: _CompanionActionContext) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "idempotency_key": action.idempotency_key,
        "tenant_id": action.tenant_id,
        "user_id": action.user_id,
        "session_id": action.session_id,
        "run_id": action.run_id,
        "agent_id": action.agent_id,
        "agent_version": action.agent_version,
        "call_id": action.call_id,
        "device_id": action.device_id,
        "envelope_version": action.envelope_version,
        "capability": action.capability,
        "tool_name": action.tool_name,
        "operation": action.operation,
        "capability_lease_id": action.capability_lease_id,
        "resource_refs": list(action.resource_refs),
        "arguments_digest": action.arguments_digest,
        "target_snapshot_digest": action.target_snapshot_digest,
        "policy_snapshot_digest": action.policy_snapshot_digest,
        "nonce": action.nonce,
        "issued_at": action.issued_at,
        "expires_at": action.expires_at,
        "platform_key_id": action.platform_key_id,
        "platform_signature": action.platform_signature,
        "approval": None if action.approval is None else action.approval.to_wire(),
        "trace_context": None,
    }


def _required_receipt_string(receipt: Mapping[str, Any], name: str) -> str:
    value = receipt.get(name)
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise PermissionError("trusted Local Node approval receipt is invalid")
    return value


def _receipt_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PermissionError("trusted Local Node approval receipt time is invalid")
    return float(value)


def _parse_companion_action(value: Mapping[str, Any]) -> _CompanionActionContext:
    required = set(_CompanionActionContext.__dataclass_fields__)
    if set(value) != required or value.get("approval") is not None:
        raise PermissionError("Local Node signed proposal shape is invalid")
    resources = value.get("resource_refs")
    trace = value.get("trace_context")
    if not isinstance(resources, list) or any(not isinstance(item, str) for item in resources):
        raise PermissionError("Local Node signed proposal resources are invalid")
    if trace is not None and not isinstance(trace, Mapping):
        raise PermissionError("Local Node signed proposal trace is invalid")
    try:
        return _CompanionActionContext(
            action_id=str(value["action_id"]),
            idempotency_key=str(value["idempotency_key"]),
            tenant_id=str(value["tenant_id"]),
            user_id=str(value["user_id"]),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]),
            agent_id=str(value["agent_id"]),
            agent_version=str(value["agent_version"]),
            call_id=str(value["call_id"]),
            device_id=str(value["device_id"]),
            envelope_version=int(value["envelope_version"]),
            capability=str(value["capability"]),
            tool_name=str(value["tool_name"]),
            operation=str(value["operation"]),
            capability_lease_id=str(value["capability_lease_id"]),
            resource_refs=tuple(resources),
            arguments_digest=str(value["arguments_digest"]),
            target_snapshot_digest=str(value["target_snapshot_digest"]),
            policy_snapshot_digest=str(value["policy_snapshot_digest"]),
            nonce=str(value["nonce"]),
            issued_at=float(value["issued_at"]),
            expires_at=float(value["expires_at"]),
            platform_key_id=str(value["platform_key_id"]),
            platform_signature=str(value["platform_signature"]),
            approval=None,
            trace_context=None if trace is None else dict(trace),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("Local Node signed proposal is invalid") from exc


class PinnedLocalNodeRunBindingResolver:
    """Explicit in-process run binding for tests/development composition only.

    It never selects a device by "first online" and never accepts a device ID
    from a model tool call.  Each complete tenant/user/session/run tuple must be
    pinned independently by the trusted composition root.
    """

    def __init__(self, bindings: Mapping[LocalNodeRunScope, LocalNodeRunBinding]) -> None:
        self._bindings = dict(bindings)

    async def resolve(self, scope: LocalNodeRunScope) -> LocalNodeRunBinding | None:
        binding = self._bindings.get(scope)
        return binding if binding is not None and binding.scope == scope else None


class SelectedLocalNodeRunBindingResolver:
    """Resolve browser selectors only after server-owned state validates them.

    Device/grant IDs choose among resources already owned by this authenticated
    tenant/user/session. They never create a grant or enlarge its capabilities.
    """

    def __init__(self, repository: LocalNodeRepository, *, lease_seconds: int = 60) -> None:
        if lease_seconds < 1 or lease_seconds > 300:
            raise ValueError("Local Node run binding lease is invalid")
        self._repository = repository
        self._lease_seconds = lease_seconds

    async def resolve(self, scope: LocalNodeRunScope) -> LocalNodeRunBinding | None:
        if (
            not scope.selected_device_id
            or not scope.selected_grant_ids
            or len(scope.selected_grant_ids) > 16
            or len(set(scope.selected_grant_ids)) != len(scope.selected_grant_ids)
            or not scope.model_provider
            or not scope.model_id
        ):
            return None
        now = datetime.now(timezone.utc)
        async with self._repository.transaction() as state:
            device = state.devices.get(scope.selected_device_id)
            if (
                device is None
                or device.tenant_id != scope.tenant_id
                or device.user_id != scope.user_id
                or device.status != "online"
                or device.revoked_at is not None
                or device.protocol_version != LOCAL_NODE_PROTOCOL_VERSION
            ):
                return None
            selected = []
            for grant_id in scope.selected_grant_ids:
                grant = state.grants.get(grant_id)
                if (
                    grant is None
                    or grant.tenant_id != scope.tenant_id
                    or grant.user_id != scope.user_id
                    or grant.device_id != device.device_id
                    or grant.revoked_at is not None
                    or (grant.expires_at is not None and grant.expires_at <= now)
                    or (grant.session_id is not None and grant.session_id != scope.session_id)
                ):
                    return None
                selected.append(grant)
        lease_payload = {
            "tenant_id": scope.tenant_id,
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "run_id": scope.run_id,
            "device_id": scope.selected_device_id,
            "grant_ids": list(scope.selected_grant_ids),
            "model_provider": scope.model_provider,
            "model_id": scope.model_id,
        }
        return LocalNodeRunBinding(
            scope=scope,
            device_id=scope.selected_device_id,
            lease_id="lease_" + _plain_digest(lease_payload)[:32],
            expires_at_ms=int(time.time() * 1000) + self._lease_seconds * 1000,
            trusted_device=True,
            model_data_egress_allowed=True,
            model_provider=scope.model_provider,
            model_id=scope.model_id,
            model_egress_purpose="assistant_local_file_analysis",
            selected_grant_ids=frozenset(scope.selected_grant_ids),
        )


def _plain_digest(value: Any) -> str:
    return str(canonical_digest(value)).removeprefix("sha256:")


def _grant_id(arguments: Mapping[str, Any]) -> str | None:
    ordinary = arguments.get("grant_id")
    app = arguments.get("app_grant_id")
    if ordinary is not None and app is not None and ordinary != app:
        return None
    candidate = ordinary if ordinary is not None else app
    return candidate if isinstance(candidate, str) and candidate else None


def _server_arguments(arguments: Mapping[str, Any], *, grant_id: str) -> dict[str, Any]:
    """Bind the control-plane grant and exact model arguments in one digest."""

    return {
        "grant_id": grant_id,
        "model_arguments": copy.deepcopy(dict(arguments)),
    }


def _resource_refs(
    *,
    envelope: LocalNodeDispatchEnvelope,
    grant_id: str,
) -> tuple[str, ...]:
    arguments = envelope.arguments
    values: tuple[Any, ...]
    if envelope.action_operation == "file.rollback":
        values = (arguments.get("rollback_ref"), grant_id, arguments.get("path"))
    elif envelope.action_capability.startswith("file."):
        values = (grant_id, arguments.get("path"))
    elif envelope.action_capability == "process.run":
        values = (grant_id, arguments.get("cwd"))
    else:
        values = (grant_id, arguments.get("window_id"))
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Local Node action resource references are incomplete")
    return tuple(str(value) for value in values)


def _target_snapshot_digest(envelope: LocalNodeDispatchEnvelope) -> str:
    """Use the exact optimistic target supplied by the typed tool contract."""

    arguments = envelope.arguments
    if envelope.action_operation == "file.write":
        return str(arguments.get("expected_sha256") or "missing")
    if envelope.action_operation == "file.rollback":
        return str(arguments.get("expected_current_sha256") or "")
    if envelope.action_operation == "app.control":
        return str(arguments.get("observation_id") or "")
    return _plain_digest(
        {
            "tool_name": envelope.tool_name,
            "operation": envelope.action_operation,
            "arguments": arguments,
        }
    )


def _expected_grant_kind(capabilities: frozenset[str]) -> str | None:
    if any(capability.startswith(("app.", "screen.", "clipboard.")) for capability in capabilities):
        return "app"
    if any(capability.startswith(("file.", "process.")) for capability in capabilities):
        return "workspace"
    if any(capability.startswith("network.") for capability in capabilities):
        return "domain"
    return None


_FILE_RESULT_OPERATIONS = frozenset(
    {"file.list", "file.read", "file.search", "file.hash", "file.watch"}
)


def _result_object(
    value: Any,
    *,
    exact_keys: frozenset[str],
    code: str = "LOCAL_NODE_INVALID_RESULT",
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != exact_keys:
        raise ValueError(code)
    return value


def _result_string(value: Any, *, maximum: int, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError("LOCAL_NODE_INVALID_RESULT")
    return value


def _result_integer(value: Any, *, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError("LOCAL_NODE_INVALID_RESULT")
    return value


def _result_sha256(value: Any) -> str:
    digest = _result_string(value, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("LOCAL_NODE_INVALID_RESULT")
    return digest


def _result_relative_path(value: Any) -> str:
    path = _result_string(value, maximum=2048)
    parts = PurePosixPath(path).parts
    if (
        path.startswith(("/", "\\"))
        or "\\" in path
        or not parts
        or ":" in parts[0]
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("LOCAL_NODE_INVALID_RESULT")
    return path


def _path_is_within(relative_path: str, requested_path: Any) -> bool:
    requested = _result_string(requested_path, maximum=2048)
    if requested == ".":
        return True
    root = PurePosixPath(requested).parts
    candidate = PurePosixPath(relative_path).parts
    return len(candidate) >= len(root) and candidate[: len(root)] == root


def validate_file_result(
    *,
    operation: str,
    arguments: Mapping[str, Any],
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate bounded, citeable file evidence at channel and AgentLoop ingress."""

    expected_kind = {
        "file.list": "file_list",
        "file.read": "file_read",
        "file.search": "file_search",
        "file.hash": "file_hash",
        "file.watch": "file_watch",
    }.get(operation)
    if expected_kind is None or value.get("kind") != expected_kind:
        raise ValueError("LOCAL_NODE_INVALID_RESULT")

    if operation == "file.list":
        result = _result_object(value, exact_keys=frozenset({"kind", "entries"}))
        entries = result["entries"]
        limit = int(arguments.get("limit", 500))
        if not isinstance(entries, list) or len(entries) > limit:
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        normalized: list[dict[str, Any]] = []
        for raw in entries:
            entry = _result_object(
                raw,
                exact_keys=frozenset({"relative_path", "kind", "size", "modified_ns"}),
            )
            relative_path = _result_relative_path(entry["relative_path"])
            if not _path_is_within(relative_path, arguments.get("path")):
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
            kind = entry["kind"]
            if kind not in {"file", "directory"}:
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
            normalized.append(
                {
                    "relative_path": relative_path,
                    "kind": kind,
                    "size": _result_integer(entry["size"]),
                    "modified_ns": _result_integer(entry["modified_ns"]),
                }
            )
        return {"kind": expected_kind, "entries": normalized}

    if operation in {"file.read", "file.hash"}:
        keys = {"kind", "relative_path", "encoding", "size", "sha256"}
        if operation == "file.read":
            keys.add("content")
        result = _result_object(value, exact_keys=frozenset(keys))
        relative_path = _result_relative_path(result["relative_path"])
        if relative_path != arguments.get("path"):
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        size = _result_integer(result["size"], maximum=8 * 1024 * 1024)
        max_bytes = int(arguments.get("max_bytes", 8 * 1024 * 1024))
        if size > max_bytes:
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        digest = _result_sha256(result["sha256"])
        encoding = result["encoding"]
        if encoding not in {"utf-8", None}:
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        normalized_file: dict[str, Any] = {
            "kind": expected_kind,
            "relative_path": relative_path,
            "encoding": encoding,
            "size": size,
            "sha256": digest,
        }
        if operation == "file.read":
            content = result["content"]
            if encoding == "utf-8":
                text = _result_string(content, maximum=1_048_576, allow_empty=True)
                encoded = text.encode("utf-8")
                if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
                    raise ValueError("LOCAL_NODE_INVALID_RESULT")
                normalized_file["content"] = text
            elif content is not None:
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
            else:
                normalized_file["content"] = None
        return normalized_file

    if operation == "file.search":
        result = _result_object(value, exact_keys=frozenset({"kind", "matches"}))
        matches = result["matches"]
        limit = int(arguments.get("limit", 200))
        if not isinstance(matches, list) or len(matches) > limit:
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        normalized_matches: list[dict[str, Any]] = []
        for raw in matches:
            match = _result_object(
                raw,
                exact_keys=frozenset({"relative_path", "line", "column", "preview", "file_sha256"}),
            )
            relative_path = _result_relative_path(match["relative_path"])
            if not _path_is_within(relative_path, arguments.get("path")):
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
            line = _result_integer(match["line"], maximum=2**31 - 1)
            column = _result_integer(match["column"], maximum=2**31 - 1)
            if line < 1 or column < 1:
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
            normalized_matches.append(
                {
                    "relative_path": relative_path,
                    "line": line,
                    "column": column,
                    "preview": _result_string(match["preview"], maximum=300, allow_empty=True),
                    "file_sha256": _result_sha256(match["file_sha256"]),
                }
            )
        return {"kind": expected_kind, "matches": normalized_matches}

    result = _result_object(value, exact_keys=frozenset({"kind", "events"}))
    events = result["events"]
    if not isinstance(events, list) or len(events) > 500:
        raise ValueError("LOCAL_NODE_INVALID_RESULT")
    normalized_events: list[dict[str, Any]] = []
    prior_sequence = 0
    for raw in events:
        event = _result_object(
            raw,
            exact_keys=frozenset(
                {
                    "sequence",
                    "kind",
                    "relative_path",
                    "previous_path",
                    "sha256",
                    "size",
                    "observed_at",
                }
            ),
        )
        sequence = _result_integer(event["sequence"], maximum=2**63 - 1)
        if sequence <= prior_sequence:
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        prior_sequence = sequence
        relative_path = _result_relative_path(event["relative_path"])
        if not _path_is_within(relative_path, arguments.get("path")):
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        event_kind = event["kind"]
        if event_kind not in {"create", "modify", "rename", "delete"}:
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        previous_path = event["previous_path"]
        if previous_path is not None:
            previous_path = _result_relative_path(previous_path)
            if not _path_is_within(previous_path, arguments.get("path")):
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
        event_digest = event["sha256"]
        event_size = event["size"]
        if event_kind == "delete":
            if event_digest is not None or event_size is not None:
                raise ValueError("LOCAL_NODE_INVALID_RESULT")
        else:
            event_digest = _result_sha256(event_digest)
            event_size = _result_integer(event_size)
        observed_at = event["observed_at"]
        if (
            isinstance(observed_at, bool)
            or not isinstance(observed_at, (int, float))
            or not math.isfinite(float(observed_at))
            or observed_at <= 0
        ):
            raise ValueError("LOCAL_NODE_INVALID_RESULT")
        normalized_events.append(
            {
                "sequence": sequence,
                "kind": event_kind,
                "relative_path": relative_path,
                "previous_path": previous_path,
                "sha256": event_digest,
                "size": event_size,
                "observed_at": float(observed_at),
            }
        )
    return {"kind": expected_kind, "events": normalized_events}


class ControlPlaneLocalNodeToolProvider(LocalNodeToolProvider):
    """Canonical provider backed by trusted control-plane state and delivery.

    The provider does not execute another planning/agent loop.  It re-resolves
    current control-plane state, signs one companion-compatible ActionContext,
    persists an idempotent dispatch fence, and hands it to the already-injected
    authenticated device delivery provider.
    """

    def __init__(
        self,
        *,
        control_plane: LocalNodeControlPlaneService,
        repository: LocalNodeRepository,
        binding_resolver: LocalNodeRunBindingResolver,
        action_signer: LocalNodePlatformActionSigner,
        approval_registrar: LocalNodeTrustedApprovalRegistrar | None = None,
        approval_receipt_verifier: LocalNodeTrustedApprovalReceiptVerifier | None = None,
        result_waiter: LocalNodeActionResultWaiter | None = None,
        agent_id: str = "assistant",
        agent_version: str = "builtin-assistant/v1",
        action_ttl_seconds: int = 30,
        result_timeout_seconds: float = 30,
    ) -> None:
        if not isinstance(binding_resolver, LocalNodeRunBindingResolver):
            raise TypeError("trusted Local Node run binding resolver is required")
        if not isinstance(action_signer, LocalNodePlatformActionSigner):
            raise TypeError("trusted Local Node platform action signer is required")
        if result_waiter is not None and not isinstance(result_waiter, LocalNodeActionResultWaiter):
            raise TypeError("trusted Local Node result waiter is invalid")
        if (
            not agent_id
            or not agent_version
            or action_ttl_seconds < 1
            or action_ttl_seconds > 60
            or isinstance(result_timeout_seconds, bool)
            or not isinstance(result_timeout_seconds, (int, float))
            or not math.isfinite(float(result_timeout_seconds))
            or result_timeout_seconds <= 0
            or result_timeout_seconds > 60
        ):
            raise ValueError("Local Node provider configuration is invalid")
        self._control_plane = control_plane
        self._repository = repository
        self._binding_resolver = binding_resolver
        self._action_signer = action_signer
        self._approval_registrar = approval_registrar
        self._approval_receipt_verifier = approval_receipt_verifier
        self._result_waiter = result_waiter
        self._agent_id = agent_id
        self._agent_version = agent_version
        self._action_ttl_seconds = action_ttl_seconds
        self._result_timeout_seconds = float(result_timeout_seconds)

    async def _binding(self, scope: LocalNodeRunScope) -> LocalNodeRunBinding | None:
        binding = await self._binding_resolver.resolve(scope)
        if (
            not isinstance(binding, LocalNodeRunBinding)
            or binding.scope != scope
            or not binding.device_id
            or not binding.lease_id
            or binding.expires_at_ms <= int(time.time() * 1000)
            or not binding.trusted_device
            or not binding.model_data_egress_allowed
            or not scope.model_provider
            or not scope.model_id
            or binding.model_provider != scope.model_provider
            or binding.model_id != scope.model_id
            or binding.model_egress_purpose != "assistant_local_file_analysis"
            or (
                scope.selected_grant_ids
                and binding.selected_grant_ids != frozenset(scope.selected_grant_ids)
            )
        ):
            return None
        return binding

    async def resolve_capabilities(
        self,
        scope: LocalNodeRunScope,
    ) -> LocalNodeCapabilitySnapshot | None:
        binding = await self._binding(scope)
        if binding is None:
            return None
        now = datetime.now(timezone.utc)
        async with self._repository.transaction() as state:
            device = state.devices.get(binding.device_id)
            if (
                device is None
                or device.tenant_id != scope.tenant_id
                or device.user_id != scope.user_id
                or device.status != "online"
                or device.revoked_at is not None
                or device.protocol_version != LOCAL_NODE_PROTOCOL_VERSION
            ):
                return None
            grants = [
                grant
                for grant in state.grants.values()
                if grant.tenant_id == scope.tenant_id
                and grant.user_id == scope.user_id
                and grant.device_id == binding.device_id
                and grant.revoked_at is None
                and (grant.expires_at is None or grant.expires_at > now)
                and (grant.session_id is None or grant.session_id == scope.session_id)
                and (not binding.selected_grant_ids or grant.grant_id in binding.selected_grant_ids)
            ]
            if not grants:
                return None
            # Catalog visibility is conservative. A compound tool is selected
            # only when one grant covers its full typed capability set; this
            # summary is therefore the union of per-grant sets for singleton
            # tools, while dispatch below rechecks one exact referenced grant.
            granted = set().union(*(set(grant.capabilities) for grant in grants))
            capabilities = frozenset(
                capability
                for capability, health in device.capabilities.items()
                if health == "ready" and capability in granted
            )
            if self._result_waiter is None:
                # A dispatch acknowledgement is not a file result.  Without an
                # authenticated terminal-result channel, read tools must be
                # invisible rather than returning a misleading queued status.
                capabilities = frozenset(
                    capability
                    for capability in capabilities
                    if capability not in {"file.list", "file.read", "file.search", "file.watch"}
                )
            if not capabilities:
                return None
            grant_revision = _plain_digest(
                [
                    {
                        "grant_id": grant.grant_id,
                        "capabilities": sorted(grant.capabilities),
                        "expires_at": grant.expires_at,
                        "session_id": grant.session_id,
                    }
                    for grant in sorted(grants, key=lambda item: item.grant_id)
                ]
            )
            return LocalNodeCapabilitySnapshot(
                scope=scope,
                device_id=binding.device_id,
                lease_id=binding.lease_id,
                grant_revision=grant_revision,
                capabilities=capabilities,
                expires_at_ms=binding.expires_at_ms,
                trusted_device=True,
                healthy=True,
                model_data_egress_allowed=True,
            )

    async def supports_capability_set(
        self,
        scope: LocalNodeRunScope,
        capabilities: frozenset[str],
    ) -> bool:
        """Require one active, correctly typed grant to cover the whole set."""

        binding = await self._binding(scope)
        expected_kind = _expected_grant_kind(capabilities)
        if (
            binding is None
            or expected_kind is None
            or not capabilities
            or (
                self._result_waiter is None
                and capabilities & {"file.list", "file.read", "file.search", "file.watch"}
            )
        ):
            return False
        now = datetime.now(timezone.utc)
        async with self._repository.transaction() as state:
            return any(
                grant.tenant_id == scope.tenant_id
                and grant.user_id == scope.user_id
                and grant.device_id == binding.device_id
                and grant.kind == expected_kind
                and grant.revoked_at is None
                and (grant.expires_at is None or grant.expires_at > now)
                and (grant.session_id is None or grant.session_id == scope.session_id)
                and (not binding.selected_grant_ids or grant.grant_id in binding.selected_grant_ids)
                and capabilities.issubset(grant.capabilities)
                for grant in state.grants.values()
            )

    async def dispatch(self, envelope: LocalNodeDispatchEnvelope) -> ToolCallResult:
        fresh = await self.resolve_capabilities(envelope.scope)
        if fresh is None:
            return self._denied(envelope, "LOCAL_NODE_UNAVAILABLE")
        if (
            fresh.device_id != envelope.device_id
            or fresh.lease_id != envelope.lease_id
            or fresh.grant_revision != envelope.grant_revision
            or not envelope.required_capabilities.issubset(fresh.capabilities)
            or not verify_local_node_gateway_receipt(
                envelope.gateway_receipt,
                tenant_id=envelope.scope.tenant_id,
                user_id=envelope.scope.user_id,
                session_id=envelope.scope.session_id,
                run_id=envelope.scope.run_id,
                tool_name=envelope.tool_name,
                arguments=envelope.arguments,
                device_id=envelope.device_id,
                lease_id=envelope.lease_id,
                grant_revision=envelope.grant_revision,
                binding_sha256=fresh.binding_sha256,
            )
        ):
            return self._denied(envelope, "LOCAL_NODE_BINDING_MISMATCH")

        grant_id = _grant_id(envelope.arguments)
        if grant_id is None:
            return self._denied(envelope, "LOCAL_NODE_ACTION_GRANT_REQUIRED")
        binding = await self._binding(envelope.scope)
        if binding is None or (
            binding.selected_grant_ids and grant_id not in binding.selected_grant_ids
        ):
            return self._denied(envelope, "LOCAL_NODE_ACTION_GRANT_DENIED")
        now = datetime.now(timezone.utc)
        expected_kind = _expected_grant_kind(envelope.required_capabilities)
        async with self._repository.transaction() as state:
            grant = state.grants.get(grant_id)
            if (
                grant is None
                or grant.tenant_id != envelope.scope.tenant_id
                or grant.user_id != envelope.scope.user_id
                or grant.device_id != envelope.device_id
                or grant.revoked_at is not None
                or (grant.expires_at is not None and grant.expires_at <= now)
                or (grant.session_id is not None and grant.session_id != envelope.scope.session_id)
                or expected_kind is None
                or grant.kind != expected_kind
                or not envelope.required_capabilities.issubset(grant.capabilities)
                or envelope.action_capability not in grant.capabilities
            ):
                return self._denied(envelope, "LOCAL_NODE_ACTION_GRANT_DENIED")
        server_arguments = _server_arguments(envelope.arguments, grant_id=grant_id)
        try:
            resource_refs = _resource_refs(envelope=envelope, grant_id=grant_id)
            target_digest = _target_snapshot_digest(envelope)
            if not target_digest:
                raise ValueError("target digest is absent")
            idempotency_key = envelope.gateway_receipt.command_id
            action_id = derive_action_id(
                tenant_id=envelope.scope.tenant_id,
                user_id=envelope.scope.user_id,
                device_id=envelope.device_id,
                idempotency_key=idempotency_key,
            )
            device_arguments = copy.deepcopy(envelope.arguments)
            action = _CompanionActionContext.create(
                action_id=action_id,
                idempotency_key=idempotency_key,
                tenant_id=envelope.scope.tenant_id,
                user_id=envelope.scope.user_id,
                session_id=envelope.scope.session_id,
                run_id=envelope.scope.run_id,
                agent_id=self._agent_id,
                agent_version=self._agent_version,
                call_id=envelope.gateway_receipt.receipt_id,
                device_id=envelope.device_id,
                envelope_version=1,
                capability=envelope.action_capability,
                tool_name=envelope.tool_name,
                operation=envelope.action_operation,
                capability_lease_id=(
                    envelope.lease_id
                    if envelope.action_capability in {"app.control", "screen.observe"}
                    else grant_id
                ),
                resource_refs=resource_refs,
                normalized_arguments=device_arguments,
                target_snapshot_digest=target_digest,
                policy_snapshot_digest=envelope.gateway_receipt.policy_sha256,
                nonce=secrets.token_urlsafe(24),
                platform_key_id=self._action_signer.key_id,
                ttl_seconds=self._action_ttl_seconds,
                approval=None,
            )
            signature = self._action_signer.sign(action.canonical_signed_payload())
            if not isinstance(signature, str) or not signature:
                raise ValueError("platform signer returned no signature")
            action = replace(action, platform_signature=signature)
            issued = datetime.fromtimestamp(action.issued_at, timezone.utc)
            control_envelope = {
                "idempotency_key": idempotency_key,
                "session_id": envelope.scope.session_id,
                "run_id": envelope.scope.run_id,
                "agent_id": self._agent_id,
                "agent_version": self._agent_version,
                "call_id": action.call_id,
                "capability": envelope.action_capability,
                "tool_name": envelope.tool_name,
                "action_operation": envelope.action_operation,
                "required_capabilities": sorted(envelope.required_capabilities),
                "normalized_arguments": server_arguments,
                "device_arguments_digest": _plain_digest(device_arguments),
                "signed_action": _companion_action_to_wire(action),
                "arguments_digest": canonical_digest(server_arguments),
                "target_snapshot_digest": target_digest,
                "policy_snapshot_digest": envelope.gateway_receipt.policy_sha256,
                "approval_id": (
                    envelope.gateway_receipt.receipt_id
                    if envelope.gateway_receipt.approval_consumed
                    else None
                ),
                "issued_at": issued,
                "expires_at": issued + timedelta(seconds=self._action_ttl_seconds),
                "trace_context": {
                    "gateway_receipt_id": envelope.gateway_receipt.receipt_id,
                    "gateway_binding_sha256": fresh.binding_sha256,
                },
            }
            authority = {
                "tenant_id": envelope.scope.tenant_id,
                "user_id": envelope.scope.user_id,
                "device_id": envelope.device_id,
                "authority_id": envelope.gateway_receipt.receipt_id,
                "envelope_digest": canonical_digest(control_envelope),
            }
            response = await self._control_plane.dispatch_action(
                tenant_id=envelope.scope.tenant_id,
                user_id=envelope.scope.user_id,
                device_id=envelope.device_id,
                envelope=control_envelope,
                dispatch_authority=authority,
            )
        except (TypeError, ValueError):
            return self._denied(envelope, "LOCAL_NODE_DISPATCH_DENIED")

        action_view = response.get("action") if isinstance(response, Mapping) else None
        if not isinstance(action_view, Mapping) or action_view.get("action_id") != action_id:
            return self._denied(envelope, "LOCAL_NODE_INVALID_ACTION_RECEIPT")
        status = str(action_view.get("status") or "unknown")
        if status == "awaiting_approval":
            if self._approval_registrar is None:
                return self._denied(envelope, "LOCAL_NODE_APPROVAL_CHANNEL_UNAVAILABLE")
            try:
                await self._approval_registrar.request_local_approval(
                    tenant_id=envelope.scope.tenant_id,
                    user_id=envelope.scope.user_id,
                    device_id=envelope.device_id,
                    action_id=action_id,
                    approval_id=envelope.gateway_receipt.receipt_id,
                    signed_action=_companion_action_to_wire(action),
                    normalized_arguments=device_arguments,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.local_node.provider_adapter.internal_failure",
                    exc,
                )
                return self._denied(envelope, "LOCAL_NODE_APPROVAL_CHANNEL_UNAVAILABLE")
        if envelope.action_operation in _FILE_RESULT_OPERATIONS:
            waiter = self._result_waiter
            if waiter is None:
                return self._result_denied(
                    envelope,
                    action_id=action_id,
                    action_status=status,
                    code="LOCAL_NODE_RESULT_CHANNEL_UNAVAILABLE",
                )
            try:
                raw_result = await waiter.await_result(
                    tenant_id=envelope.scope.tenant_id,
                    user_id=envelope.scope.user_id,
                    device_id=envelope.device_id,
                    action_id=action_id,
                    timeout_seconds=self._result_timeout_seconds,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.local_node.provider_adapter.internal_failure",
                    exc,
                )
                raw_result = None
            if raw_result is None:
                return self._result_denied(
                    envelope,
                    action_id=action_id,
                    action_status=status,
                    code="LOCAL_NODE_RESULT_UNAVAILABLE",
                )
            try:
                typed_result = validate_file_result(
                    operation=envelope.action_operation,
                    arguments=envelope.arguments,
                    value=raw_result,
                )
            except (TypeError, ValueError):
                return self._result_denied(
                    envelope,
                    action_id=action_id,
                    action_status=status,
                    code="LOCAL_NODE_INVALID_RESULT",
                )
            return ToolCallResult(
                call_id=envelope.gateway_receipt.receipt_id,
                tool_name=envelope.tool_name,
                success=True,
                result=typed_result,
                metadata={
                    "action_id": action_id,
                    "action_status": "succeeded",
                    "device_result_authenticated": True,
                    "side_effect_state": "not_applicable",
                    "blind_replay_allowed": False,
                },
            )
        return ToolCallResult(
            call_id=envelope.gateway_receipt.receipt_id,
            tool_name=envelope.tool_name,
            success=status in {"dispatched", "running", "observed", "succeeded"},
            result={
                "action_id": action_id,
                "status": status,
                "local_approval_required": status == "awaiting_approval",
            },
            metadata={
                "action_id": action_id,
                "action_status": status,
                "side_effect_state": "not_started" if status == "awaiting_approval" else status,
                "blind_replay_allowed": False,
            },
        )

    async def record_trusted_local_approval(
        self,
        *,
        scope: LocalNodeRunScope,
        action_id: str,
        channel: LocalNodeDeviceChannelPrincipal,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Release an awaiting action only after independent local verification.

        A browser/Gateway approval cannot call this successfully: the channel
        principal and local receipt verifier are injected device authorities.
        """

        binding = await self._binding(scope)
        verifier = self._approval_receipt_verifier
        if (
            binding is None
            or verifier is None
            or channel.tenant_id != scope.tenant_id
            or channel.user_id != scope.user_id
            or channel.device_id != binding.device_id
            or not channel.channel_id
            or await verifier.verify(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                device_id=binding.device_id,
                action_id=action_id,
                receipt=receipt,
            )
            is not True
        ):
            raise PermissionError("trusted Local Node approval receipt is invalid")
        action_state = await self._control_plane.get_action(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            device_id=binding.device_id,
            action_id=action_id,
        )
        action_view = action_state.get("action") if isinstance(action_state, Mapping) else None
        if not isinstance(action_view, Mapping) or action_view.get("status") != "awaiting_approval":
            raise PermissionError("Local Node action is not awaiting local approval")
        approved_action = await self._finalize_signed_action(
            scope=scope,
            binding=binding,
            action_id=action_id,
            receipt=receipt,
        )
        receipt_copy = dict(receipt)
        receipt_copy["finalized_signed_action"] = approved_action
        result = await self._control_plane.record_approval_receipt(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            device_id=binding.device_id,
            action_id=action_id,
            channel=channel,
            receipt=receipt_copy,
        )
        return dict(result)

    async def _finalize_signed_action(
        self,
        *,
        scope: LocalNodeRunScope,
        binding: LocalNodeRunBinding,
        action_id: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freshly authorize and re-sign the exact proposal plus local receipt."""

        async with self._repository.transaction() as state:
            persisted = state.actions.get(action_id)
            if (
                persisted is None
                or persisted.tenant_id != scope.tenant_id
                or persisted.user_id != scope.user_id
                or persisted.device_id != binding.device_id
                or persisted.session_id != scope.session_id
                or persisted.run_id != scope.run_id
                or persisted.status != "awaiting_approval"
            ):
                raise PermissionError("Local Node approval action binding changed")
            envelope = copy.deepcopy(persisted.envelope)
            signed_raw = envelope.get("signed_action")
            server_arguments = envelope.get("normalized_arguments")
            if not isinstance(signed_raw, Mapping) or not isinstance(server_arguments, Mapping):
                raise PermissionError("Local Node approval proposal is invalid")
            grant_id = server_arguments.get("grant_id")
            model_arguments = server_arguments.get("model_arguments")
            required = envelope.get("required_capabilities")
            if (
                not isinstance(grant_id, str)
                or not isinstance(model_arguments, Mapping)
                or not isinstance(required, list)
                or any(not isinstance(value, str) for value in required)
            ):
                raise PermissionError("Local Node approval proposal is invalid")
            required_set = frozenset(required)
            grant = state.grants.get(grant_id)
            device = state.devices.get(binding.device_id)
            now = datetime.now(timezone.utc)
            expected_kind = _expected_grant_kind(required_set)
            if (
                grant is None
                or device is None
                or device.status != "online"
                or device.revoked_at is not None
                or grant.tenant_id != scope.tenant_id
                or grant.user_id != scope.user_id
                or grant.device_id != binding.device_id
                or grant.revoked_at is not None
                or (grant.expires_at is not None and grant.expires_at <= now)
                or (grant.session_id is not None and grant.session_id != scope.session_id)
                or expected_kind is None
                or grant.kind != expected_kind
                or not required_set.issubset(grant.capabilities)
                or any(device.capabilities.get(value) != "ready" for value in required_set)
            ):
                raise PermissionError("Local Node approval authority was revoked")
            proposal = _parse_companion_action(signed_raw)
            if (
                proposal.action_id != action_id
                or proposal.tenant_id != scope.tenant_id
                or proposal.user_id != scope.user_id
                or proposal.session_id != scope.session_id
                or proposal.run_id != scope.run_id
                or proposal.device_id != binding.device_id
                or proposal.capability != envelope.get("capability")
                or proposal.tool_name != envelope.get("tool_name")
                or proposal.operation != envelope.get("action_operation")
                or proposal.arguments_digest != _plain_digest(model_arguments)
                or proposal.target_snapshot_digest != envelope.get("target_snapshot_digest")
                or proposal.policy_snapshot_digest != envelope.get("policy_snapshot_digest")
                or proposal.approval is not None
            ):
                raise PermissionError("Local Node approval intent changed")

        proof = LocalNodeTrustedApprovalProof(
            approval_id=_required_receipt_string(receipt, "approval_id"),
            action_id=_required_receipt_string(receipt, "action_id"),
            device_id=_required_receipt_string(receipt, "device_id"),
            arguments_digest=_required_receipt_string(receipt, "device_arguments_digest"),
            target_snapshot_digest=_required_receipt_string(receipt, "target_snapshot_digest"),
            policy_snapshot_digest=_required_receipt_string(receipt, "policy_snapshot_digest"),
            nonce=_required_receipt_string(receipt, "decision_nonce"),
            expires_at=_receipt_timestamp(receipt.get("expires_at")),
            local_signature=_required_receipt_string(receipt, "local_signature"),
        )
        if (
            receipt.get("approved") is not True
            or proof.action_id != proposal.action_id
            or proof.device_id != proposal.device_id
            or proof.arguments_digest != proposal.arguments_digest
            or proof.target_snapshot_digest != proposal.target_snapshot_digest
            or proof.policy_snapshot_digest != proposal.policy_snapshot_digest
        ):
            raise PermissionError("Local Node approval receipt changed the intent")
        finalized = replace(proposal, approval=proof, platform_signature="")
        signature = self._action_signer.sign(finalized.canonical_signed_payload())
        if not isinstance(signature, str) or not signature:
            raise PermissionError("platform action re-signing failed")
        return _companion_action_to_wire(replace(finalized, platform_signature=signature))

    @staticmethod
    def _result_denied(
        envelope: LocalNodeDispatchEnvelope,
        *,
        action_id: str,
        action_status: str,
        code: str,
    ) -> ToolCallResult:
        return ToolCallResult(
            call_id=envelope.gateway_receipt.receipt_id,
            tool_name=envelope.tool_name,
            success=False,
            error=code,
            metadata={
                "execution_surface": "local_node",
                "action_id": action_id,
                "action_status": action_status,
                "execution_authorized": True,
                "side_effect_state": "not_applicable",
                "blind_replay_allowed": False,
            },
        )

    @staticmethod
    def _denied(envelope: LocalNodeDispatchEnvelope, code: str) -> ToolCallResult:
        return ToolCallResult(
            call_id=envelope.gateway_receipt.receipt_id,
            tool_name=envelope.tool_name,
            success=False,
            error=code,
            metadata={
                "execution_surface": "local_node",
                "side_effect_state": "not_started",
                "blind_replay_allowed": False,
            },
        )


__all__ = [
    "ControlPlaneLocalNodeToolProvider",
    "LocalNodeDeviceChannelPrincipal",
    "LocalNodeActionResultWaiter",
    "LocalNodePlatformActionSigner",
    "LocalNodeTrustedApprovalRegistrar",
    "LocalNodeTrustedApprovalReceiptVerifier",
    "LocalNodeRunBinding",
    "LocalNodeRunBindingResolver",
    "PinnedLocalNodeRunBindingResolver",
    "SelectedLocalNodeRunBindingResolver",
    "validate_file_result",
]
