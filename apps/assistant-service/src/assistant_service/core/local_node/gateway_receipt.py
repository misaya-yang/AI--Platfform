"""Unforgeable, process-local receipts for canonical Local Node dispatch.

Remote clients can set JSON metadata such as ``execution_gateway_approved``.
They cannot construct this frozen receipt or its HMAC.  The canonical
``AssistantExecutionGateway`` issues it only after policy, approval, command
claim, and final dispatch authorization have completed.

The receipt is the trusted adapter seam, not a device transport credential.
A production provider must translate it into the paired device's signed
transport envelope; this package intentionally does not invent that transport.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, cast

_RECEIPT_VERSION = "assistant-local-node-gateway/v1"
_RECEIPT_TTL_MS = 30_000
_PROCESS_RECEIPT_KEY = secrets.token_bytes(32)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalNodeGatewayReceipt:
    """Scope- and argument-bound proof that the canonical Gateway authorized dispatch."""

    version: str
    receipt_id: str
    issued_at_ms: int
    expires_at_ms: int
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments_sha256: str
    device_id: str
    lease_id: str
    grant_revision: str
    binding_sha256: str
    command_id: str
    command_durability: str
    policy_sha256: str
    sandbox_sha256: str
    approval_consumed: bool
    approval_ref_sha256: str
    gateway_signature: str

    def signed_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("gateway_signature", None)
        return payload

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def issue_local_node_gateway_receipt(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    device_id: str,
    lease_id: str,
    grant_revision: str,
    binding_sha256: str,
    command_id: str,
    command_durability: str,
    policy_decision: dict[str, Any],
    sandbox_decision: dict[str, Any],
    approval_consumed: bool,
    approval_id: str,
) -> LocalNodeGatewayReceipt:
    """Issue a short-lived receipt after the Gateway's final dispatch fence."""

    required = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "run_id": run_id,
        "tool_name": tool_name,
        "device_id": device_id,
        "lease_id": lease_id,
        "grant_revision": grant_revision,
        "binding_sha256": binding_sha256,
        "command_id": command_id,
    }
    if any(not str(value or "") for value in required.values()):
        raise ValueError("Local Node gateway receipt is missing a required binding")
    if policy_decision.get("allowed") is not True:
        raise ValueError("Local Node gateway receipt requires an allowed policy decision")
    if sandbox_decision.get("allowed") is not True:
        raise ValueError("Local Node gateway receipt requires an allowed sandbox decision")

    issued_at_ms = int(time.time() * 1000)
    unsigned = {
        "version": _RECEIPT_VERSION,
        "receipt_id": f"lngr_{secrets.token_hex(16)}",
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": issued_at_ms + _RECEIPT_TTL_MS,
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "session_id": str(session_id),
        "run_id": str(run_id),
        "tool_name": str(tool_name),
        "arguments_sha256": _canonical_digest(arguments),
        "device_id": str(device_id),
        "lease_id": str(lease_id),
        "grant_revision": str(grant_revision),
        "binding_sha256": str(binding_sha256),
        "command_id": str(command_id),
        "command_durability": str(command_durability),
        "policy_sha256": _canonical_digest(policy_decision),
        "sandbox_sha256": _canonical_digest(sandbox_decision),
        "approval_consumed": bool(approval_consumed),
        "approval_ref_sha256": (
            hashlib.sha256(str(approval_id).encode("utf-8")).hexdigest() if approval_id else ""
        ),
    }
    signature = hmac.new(
        _PROCESS_RECEIPT_KEY,
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return LocalNodeGatewayReceipt(
        **cast(Any, unsigned),
        gateway_signature=signature,
    )


def verify_local_node_gateway_receipt(
    receipt: Any,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    device_id: str,
    lease_id: str,
    grant_revision: str,
    binding_sha256: str,
) -> bool:
    """Verify type, HMAC, freshness, exact identity, arguments, and grant binding."""

    if not isinstance(receipt, LocalNodeGatewayReceipt):
        return False
    if receipt.version != _RECEIPT_VERSION or receipt.expires_at_ms < int(time.time() * 1000):
        return False
    expected_values = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "run_id": run_id,
        "tool_name": tool_name,
        "arguments_sha256": _canonical_digest(arguments),
        "device_id": device_id,
        "lease_id": lease_id,
        "grant_revision": grant_revision,
        "binding_sha256": binding_sha256,
    }
    if any(str(getattr(receipt, name)) != str(value) for name, value in expected_values.items()):
        return False
    expected_signature = hmac.new(
        _PROCESS_RECEIPT_KEY,
        json.dumps(
            receipt.signed_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(receipt.gateway_signature, expected_signature)
