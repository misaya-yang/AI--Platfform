"""Security invariants for the Local Node control-plane state machine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.api.routes.local_nodes import LocalNodeServiceFault
from assistant_service.core.local_node import (
    InMemoryLocalNodeRepository,
    LocalNodeControlPlaneService,
    canonical_digest,
    derive_action_id,
    wire_local_node_control_plane,
)
from assistant_service.core.local_node.protocol import LOCAL_NODE_PROTOCOL_VERSION
from fastapi import FastAPI


class _Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-{self.value}"


class _Delivery:
    idempotent_enqueue = True

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def enqueue_action(self, **values: Any) -> str:
        self.enqueued.append(values)
        return f"delivery-{values['action_id']}"

    async def cancel_action(self, **values: Any) -> None:
        self.cancelled.append(values["action_id"])


class _CrashAfterAcceptDelivery(_Delivery):
    def __init__(self) -> None:
        super().__init__()
        self.accepted: dict[str, str] = {}
        self._crash_once = True

    async def enqueue_action(self, **values: Any) -> str:
        self.enqueued.append(values)
        action_id = values["action_id"]
        envelope_digest = values["envelope_digest"]
        previous = self.accepted.setdefault(action_id, envelope_digest)
        if previous != envelope_digest:
            raise RuntimeError("provider idempotency conflict")
        if self._crash_once:
            self._crash_once = False
            raise RuntimeError("connection lost after provider acceptance")
        return f"delivery-{action_id}"


def _channel(
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
    device_id: str = "device-a",
    channel_id: str = "channel-a",
) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        device_id=device_id,
        channel_id=channel_id,
    )


def _service() -> tuple[LocalNodeControlPlaneService, _Delivery]:
    delivery = _Delivery()
    service = LocalNodeControlPlaneService(
        repository=InMemoryLocalNodeRepository(purpose="test"),
        action_provider=delivery,
        id_factory=_Ids(),
        user_code_factory=lambda: "123456",
    )
    return service, delivery


async def _pair(
    service: LocalNodeControlPlaneService,
    *,
    claims: list[str] | None = None,
) -> tuple[dict[str, Any], SimpleNamespace]:
    challenge = await service.create_pairing_challenge(
        tenant_id="tenant-a",
        user_id="user-a",
        ttl_seconds=180,
    )
    channel = _channel()
    device = await service.complete_pairing(
        tenant_id="tenant-a",
        user_id="user-a",
        challenge_id=challenge["challenge"]["challenge_id"],
        channel=channel,
        display_name="Local Mac",
        platform="macos",
        node_version="0.1.0",
        protocol_version=LOCAL_NODE_PROTOCOL_VERSION,
        capability_claims=claims or ["file.read"],
        permission_snapshot_digest="sha256:" + "a" * 64,
    )
    return device, channel


async def _ready_grant(
    service: LocalNodeControlPlaneService,
) -> tuple[SimpleNamespace, str]:
    _, channel = await _pair(service)
    await service.record_capability_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        revision=1,
        capabilities={"file.read": "ready"},
    )
    result = await service.create_grant(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        kind="workspace",
        channel=channel,
        grant={
            "display_name": "Workspace",
            "resource_ref": "workspace-ref",
            "capabilities": ["file.read"],
            "session_id": "session-a",
            "expires_at": None,
        },
    )
    return channel, result["grant"]["grant_id"]


def _envelope(
    grant_id: str,
    *,
    idempotency_key: str = "idem-a",
    approval_id: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    arguments = {"grant_id": grant_id, "path_ref": "relative-file-ref"}
    envelope: dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "session_id": "session-a",
        "run_id": "run-a",
        "call_id": "call-a",
        "capability": "file.read",
        "required_capabilities": ["file.read"],
        "normalized_arguments": arguments,
        "arguments_digest": canonical_digest(arguments),
        "target_snapshot_digest": "sha256:" + "b" * 64,
        "policy_snapshot_digest": "sha256:" + "c" * 64,
        "approval_id": approval_id,
        "issued_at": now,
        "expires_at": now + timedelta(minutes=2),
        "trace_context": {},
    }
    if approval_id is not None:
        envelope["tool_name"] = "local_file_read"
        envelope["action_operation"] = "file.read"
        envelope["signed_action"] = {
            "action_id": derive_action_id(
                tenant_id="tenant-a",
                user_id="user-a",
                device_id="device-a",
                idempotency_key=idempotency_key,
            ),
            "idempotency_key": idempotency_key,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "run_id": "run-a",
            "call_id": "call-a",
            "device_id": "device-a",
            "capability": "file.read",
            "tool_name": "local_file_read",
            "operation": "file.read",
            "arguments_digest": envelope["arguments_digest"],
            "target_snapshot_digest": envelope["target_snapshot_digest"],
            "policy_snapshot_digest": envelope["policy_snapshot_digest"],
            "platform_signature": "proposal-platform-signature",
            "approval": None,
        }
    return envelope


def _authority(envelope: dict[str, Any], **overrides: str) -> SimpleNamespace:
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "device_id": "device-a",
        "authority_id": "execution-gateway-receipt-a",
        "envelope_digest": canonical_digest(envelope),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _approval_receipt(
    envelope: dict[str, Any],
    *,
    approved: bool = True,
    nonce: str = "approval-nonce-a",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=2)
    receipt = {
        "approval_id": envelope["approval_id"],
        "approved": approved,
        "arguments_digest": envelope["arguments_digest"],
        "target_snapshot_digest": envelope["target_snapshot_digest"],
        "policy_snapshot_digest": envelope["policy_snapshot_digest"],
        "decision_nonce": nonce,
        "decided_at": now,
        "expires_at": expires_at,
        "local_signature": "trusted-local-signature",
        "reason_code": None,
    }
    if approved:
        proposal = envelope["signed_action"]
        receipt["finalized_signed_action"] = {
            **proposal,
            "platform_signature": "approved-platform-signature",
            "approval": {
                "approval_id": envelope["approval_id"],
                "action_id": proposal["action_id"],
                "device_id": proposal["device_id"],
                "arguments_digest": receipt["arguments_digest"],
                "target_snapshot_digest": receipt["target_snapshot_digest"],
                "policy_snapshot_digest": receipt["policy_snapshot_digest"],
                "nonce": nonce,
                "expires_at": expires_at.timestamp(),
                "local_signature": receipt["local_signature"],
            },
        }
    return receipt


@pytest.mark.asyncio
async def test_pairing_is_single_use_and_owner_is_not_disclosed() -> None:
    service, _ = _service()
    challenge = await service.create_pairing_challenge(
        tenant_id="tenant-a",
        user_id="user-a",
        ttl_seconds=180,
    )
    challenge_id = challenge["challenge"]["challenge_id"]
    values = {
        "challenge_id": challenge_id,
        "channel": _channel(),
        "display_name": "Mac",
        "platform": "macos",
        "node_version": "0.1.0",
        "protocol_version": LOCAL_NODE_PROTOCOL_VERSION,
        "capability_claims": ["file.read"],
        "permission_snapshot_digest": "sha256:" + "a" * 64,
    }
    await service.complete_pairing(tenant_id="tenant-a", user_id="user-a", **values)

    with pytest.raises(LocalNodeServiceFault) as replay:
        await service.complete_pairing(tenant_id="tenant-a", user_id="user-a", **values)
    assert replay.value.status_code == 409
    assert replay.value.code == "LOCAL_NODE_PAIRING_REPLAYED"

    with pytest.raises(LocalNodeServiceFault) as cross_tenant:
        await service.get_device_status(
            tenant_id="tenant-b",
            user_id="user-a",
            device_id="device-a",
        )
    assert cross_tenant.value.status_code == 404
    assert await service.list_devices(tenant_id="tenant-b", user_id="user-a") == {"devices": []}


@pytest.mark.asyncio
async def test_pairing_accepts_only_preverified_channel_principal_contract() -> None:
    """The API verifier owns user-code proof; service still binds owner/channel."""

    service, _ = _service()
    challenge = await service.create_pairing_challenge(
        tenant_id="tenant-a", user_id="user-a", ttl_seconds=180
    )
    with pytest.raises(LocalNodeServiceFault) as unverified:
        await service.complete_pairing(
            tenant_id="tenant-a",
            user_id="user-a",
            challenge_id=challenge["challenge"]["challenge_id"],
            channel=SimpleNamespace(),
            display_name="Mac",
            platform="macos",
            node_version="0.1.0",
            protocol_version=LOCAL_NODE_PROTOCOL_VERSION,
            capability_claims=["app.control", "screen.observe"],
            permission_snapshot_digest="sha256:" + "a" * 64,
        )
    assert unverified.value.code == "LOCAL_NODE_CHANNEL_OWNER_MISMATCH"


@pytest.mark.asyncio
async def test_realtime_health_recovers_within_pairing_ceiling_only() -> None:
    service, _ = _service()
    _, channel = await _pair(service, claims=["file.read", "process.run"])
    await service.record_capability_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        revision=1,
        capabilities={"file.read": "ready", "process.run": "denied"},
    )
    await service.record_capability_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        revision=2,
        capabilities={"file.read": "ready", "process.run": "ready"},
    )
    recovered = await service.get_device_capabilities(
        tenant_id="tenant-a", user_id="user-a", device_id="device-a"
    )
    assert {item["name"]: item["state"] for item in recovered["capabilities"]} == {
        "file.read": "ready",
        "process.run": "ready",
    }

    with pytest.raises(LocalNodeServiceFault) as expansion:
        await service.record_capability_snapshot(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            channel=channel,
            revision=3,
            capabilities={
                "file.read": "ready",
                "process.run": "ready",
                "app.control": "ready",
            },
        )
    assert expansion.value.code == "LOCAL_NODE_CAPABILITY_EXPANSION_DENIED"


@pytest.mark.asyncio
async def test_revoke_cascades_and_channel_credential_is_bound() -> None:
    service, _ = _service()
    _, channel = await _ready_grant(service)
    with pytest.raises(LocalNodeServiceFault) as wrong_channel:
        await service.create_grant(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            kind="workspace",
            channel=_channel(channel_id="forged-channel"),
            grant={
                "display_name": "Forged",
                "resource_ref": "workspace-ref",
                "capabilities": ["file.read"],
            },
        )
    assert wrong_channel.value.code == "LOCAL_NODE_CHANNEL_CREDENTIAL_MISMATCH"

    await service.revoke_device(tenant_id="tenant-a", user_id="user-a", device_id="device-a")
    grants = await service.list_grants(tenant_id="tenant-a", user_id="user-a", device_id="device-a")
    assert [grant["status"] for grant in grants["grants"]] == ["revoked"]
    with pytest.raises(LocalNodeServiceFault) as revoked:
        await service.record_capability_snapshot(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            channel=channel,
            revision=2,
            capabilities={"file.read": "ready"},
        )
    assert revoked.value.status_code == 410


@pytest.mark.asyncio
async def test_action_requires_exact_authority_and_is_idempotent() -> None:
    service, delivery = _service()
    _, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id)

    with pytest.raises(LocalNodeServiceFault) as unsigned:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=envelope,
        )
    assert unsigned.value.code == "LOCAL_NODE_DISPATCH_AUTHORITY_MISSING"

    with pytest.raises(LocalNodeServiceFault) as forged:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=envelope,
            dispatch_authority=_authority(envelope, tenant_id="tenant-b"),
        )
    assert forged.value.code == "LOCAL_NODE_DISPATCH_AUTHORITY_MISMATCH"

    first = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    repeated = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    assert first == repeated
    assert len(delivery.enqueued) == 1
    assert delivery.enqueued[0]["envelope_digest"] == canonical_digest(envelope)

    # The idempotency fence binds the complete canonical envelope, not merely
    # normalized arguments.  A changed call binding must conflict.
    changed_envelope = {**envelope, "call_id": "call-other"}
    with pytest.raises(LocalNodeServiceFault) as conflict:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=changed_envelope,
            dispatch_authority=_authority(changed_envelope),
        )
    assert conflict.value.code == "LOCAL_NODE_IDEMPOTENCY_CONFLICT"
    assert len(delivery.enqueued) == 1


@pytest.mark.asyncio
async def test_dispatch_fence_precedes_idempotent_external_enqueue() -> None:
    provider = _CrashAfterAcceptDelivery()
    service = LocalNodeControlPlaneService(
        repository=InMemoryLocalNodeRepository(purpose="test"),
        action_provider=provider,
        id_factory=_Ids(),
        user_code_factory=lambda: "123456",
    )
    _, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id)

    with pytest.raises(LocalNodeServiceFault) as lost_ack:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=envelope,
            dispatch_authority=_authority(envelope),
        )
    assert lost_ack.value.code == "LOCAL_NODE_ACTION_DELIVERY_UNAVAILABLE"
    action_id = next(iter(provider.accepted))
    fenced = await service.get_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id=action_id,
    )
    assert fenced["action"]["status"] == "policy_check"

    retried = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    assert retried["action"]["status"] == "dispatched"
    assert len(provider.accepted) == 1
    assert len(provider.enqueued) == 2
    assert {call["action_id"] for call in provider.enqueued} == {action_id}
    assert {call["envelope_digest"] for call in provider.enqueued} == {canonical_digest(envelope)}


@pytest.mark.asyncio
async def test_device_local_approval_gates_delivery_exactly_once() -> None:
    service, delivery = _service()
    channel, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id, approval_id="approval-local-a")
    proposed = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    action_id = proposed["action"]["action_id"]
    assert proposed["action"]["status"] == "awaiting_approval"
    assert delivery.enqueued == []

    receipt = _approval_receipt(envelope)
    approved = await service.record_approval_receipt(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id=action_id,
        channel=channel,
        receipt=receipt,
    )
    assert approved["action_status"] == "dispatched"
    assert len(delivery.enqueued) == 1

    with pytest.raises(LocalNodeServiceFault) as replay:
        await service.record_approval_receipt(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            action_id=action_id,
            channel=channel,
            receipt=receipt,
        )
    assert replay.value.code == "LOCAL_NODE_APPROVAL_REPLAYED"
    assert len(delivery.enqueued) == 1


@pytest.mark.asyncio
async def test_local_approval_mutation_cross_device_and_denial_never_deliver() -> None:
    service, delivery = _service()
    channel, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id, approval_id="approval-local-a")
    proposed = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    action_id = proposed["action"]["action_id"]

    with pytest.raises(LocalNodeServiceFault) as cross_device:
        await service.record_approval_receipt(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            action_id=action_id,
            channel=_channel(device_id="device-other"),
            receipt=_approval_receipt(envelope),
        )
    assert cross_device.value.status_code == 403
    assert delivery.enqueued == []

    mutated = _approval_receipt(envelope, nonce="nonce-mutated")
    mutated["arguments_digest"] = "sha256:" + "f" * 64
    with pytest.raises(LocalNodeServiceFault) as digest_mismatch:
        await service.record_approval_receipt(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            action_id=action_id,
            channel=channel,
            receipt=mutated,
        )
    assert digest_mismatch.value.code == "LOCAL_NODE_APPROVAL_DIGEST_MISMATCH"
    assert delivery.enqueued == []

    denied = await service.record_approval_receipt(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id=action_id,
        channel=channel,
        receipt=_approval_receipt(envelope, approved=False, nonce="nonce-deny"),
    )
    assert denied["action_status"] == "cancelled"
    assert delivery.enqueued == []


@pytest.mark.asyncio
async def test_gateway_or_web_replay_cannot_bypass_pending_local_approval() -> None:
    service, delivery = _service()
    _, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id, approval_id="approval-local-a")
    first = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    repeated = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    assert first == repeated
    assert repeated["action"]["status"] == "awaiting_approval"
    assert delivery.enqueued == []


@pytest.mark.asyncio
@pytest.mark.parametrize("time_case", ["expired", "future", "overlong", "naive"])
async def test_direct_action_dispatch_revalidates_time_window(time_case: str) -> None:
    service, delivery = _service()
    _, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id)
    now = datetime.now(timezone.utc)
    if time_case == "expired":
        envelope["issued_at"] = now - timedelta(minutes=3)
        envelope["expires_at"] = now - timedelta(minutes=1)
    elif time_case == "future":
        envelope["issued_at"] = now + timedelta(minutes=3)
        envelope["expires_at"] = now + timedelta(minutes=4)
    elif time_case == "overlong":
        envelope["issued_at"] = now
        envelope["expires_at"] = now + timedelta(minutes=11)
    else:
        envelope["issued_at"] = datetime.now()
        envelope["expires_at"] = datetime.now() + timedelta(minutes=1)

    with pytest.raises(LocalNodeServiceFault) as invalid:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=envelope,
            dispatch_authority=_authority(envelope),
        )
    assert invalid.value.status_code == 422
    assert delivery.enqueued == []


@pytest.mark.asyncio
async def test_every_action_requires_grant_id_including_app_control() -> None:
    service, delivery = _service()
    _, channel = await _pair(service, claims=["app.control", "screen.observe"])
    await service.record_capability_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        revision=1,
        capabilities={"app.control": "ready", "screen.observe": "ready"},
    )
    now = datetime.now(timezone.utc)
    arguments = {"app_ref": "app-ref", "action": "click"}
    envelope = {
        "idempotency_key": "idem-app",
        "session_id": "session-a",
        "run_id": "run-a",
        "call_id": "call-a",
        "capability": "app.control",
        "required_capabilities": ["app.control"],
        "normalized_arguments": arguments,
        "arguments_digest": canonical_digest(arguments),
        "target_snapshot_digest": "sha256:" + "b" * 64,
        "policy_snapshot_digest": "sha256:" + "c" * 64,
        "approval_id": None,
        "issued_at": now,
        "expires_at": now + timedelta(minutes=2),
        "trace_context": {},
    }
    with pytest.raises(LocalNodeServiceFault) as missing_grant:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=envelope,
            dispatch_authority=_authority(envelope),
        )
    assert missing_grant.value.code == "LOCAL_NODE_ACTION_GRANT_REQUIRED"
    assert delivery.enqueued == []


@pytest.mark.asyncio
async def test_compound_capabilities_cannot_be_assembled_across_grants() -> None:
    service, delivery = _service()
    _, channel = await _pair(
        service,
        claims=["app.observe", "screen.observe", "screen.share"],
    )
    await service.record_capability_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        revision=1,
        capabilities={
            "app.observe": "ready",
            "screen.observe": "ready",
            "screen.share": "ready",
        },
    )
    first = await service.create_grant(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        kind="app",
        channel=channel,
        grant={
            "display_name": "Observed App",
            "app_ref": "app-ref-a",
            "capabilities": ["app.observe", "screen.observe"],
            "session_id": "session-a",
        },
    )
    await service.create_grant(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        kind="app",
        channel=channel,
        grant={
            "display_name": "Share-only App",
            "app_ref": "app-ref-b",
            "capabilities": ["screen.share"],
            "session_id": "session-a",
        },
    )
    grant_id = first["grant"]["grant_id"]
    envelope = _envelope(grant_id)
    arguments = {"grant_id": grant_id, "window_id": "window-a"}
    envelope.update(
        {
            "capability": "screen.observe",
            "required_capabilities": [
                "app.observe",
                "screen.observe",
                "screen.share",
            ],
            "normalized_arguments": arguments,
            "arguments_digest": canonical_digest(arguments),
        }
    )
    with pytest.raises(LocalNodeServiceFault) as split_grant:
        await service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=envelope,
            dispatch_authority=_authority(envelope),
        )
    assert split_grant.value.code == "LOCAL_NODE_ACTION_CAPABILITY_DENIED"
    assert delivery.enqueued == []


@pytest.mark.asyncio
async def test_event_order_and_unique_terminal_are_atomic() -> None:
    service, _ = _service()
    channel, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id)
    dispatched = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    action_id = dispatched["action"]["action_id"]
    now = datetime.now(timezone.utc)
    result = await service.append_events(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        events=[
            {
                "event_id": "event-1",
                "sequence": 1,
                "event_type": "action.running",
                "occurred_at": now,
                "action_id": action_id,
                "status": "running",
            },
            {
                "event_id": "event-2",
                "sequence": 2,
                "event_type": "action.succeeded",
                "occurred_at": now,
                "action_id": action_id,
                "status": "succeeded",
            },
        ],
    )
    assert result["accepted_through_sequence"] == 2

    with pytest.raises(LocalNodeServiceFault) as second_terminal:
        await service.append_events(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            channel=channel,
            events=[
                {
                    "event_id": "event-3",
                    "sequence": 3,
                    "event_type": "action.failed",
                    "occurred_at": now,
                    "action_id": action_id,
                    "status": "failed",
                }
            ],
        )
    assert second_terminal.value.code == "LOCAL_NODE_ACTION_TERMINAL_CONFLICT"
    events = await service.list_events(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        after_sequence=0,
        limit=100,
    )
    assert [event["sequence"] for event in events["events"]] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "status"),
    [("device.supplied-string", None), ("action.running", "succeeded")],
)
async def test_device_events_use_allowlist_and_matching_shape(
    event_type: str,
    status: str | None,
) -> None:
    service, _ = _service()
    channel, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id)
    dispatched = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    with pytest.raises(LocalNodeServiceFault) as denied:
        await service.append_events(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            channel=channel,
            events=[
                {
                    "event_id": "event-untrusted",
                    "sequence": 1,
                    "event_type": event_type,
                    "occurred_at": datetime.now(timezone.utc),
                    "action_id": dispatched["action"]["action_id"],
                    "status": status,
                }
            ],
        )
    assert denied.value.status_code == 422
    events = await service.list_events(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        after_sequence=0,
        limit=100,
    )
    assert events["events"] == []


@pytest.mark.asyncio
async def test_device_event_artifacts_are_opaque_refs_not_paths_or_content() -> None:
    service, _ = _service()
    channel, grant_id = await _ready_grant(service)
    envelope = _envelope(grant_id)
    dispatched = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    with pytest.raises(LocalNodeServiceFault) as raw_path:
        await service.append_events(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            channel=channel,
            events=[
                {
                    "event_id": "event-path-leak",
                    "sequence": 1,
                    "event_type": "action.observed",
                    "occurred_at": datetime.now(timezone.utc),
                    "action_id": dispatched["action"]["action_id"],
                    "status": "observed",
                    "artifact_refs": ["/private/tmp/user-document.txt"],
                }
            ],
        )
    assert raw_path.value.code == "LOCAL_NODE_ARTIFACT_REF_INVALID"
    assert (
        await service.list_events(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            after_sequence=0,
            limit=100,
        )
    )["events"] == []


def test_wiring_requires_every_trusted_dependency_and_rejects_prod_memory() -> None:
    app = FastAPI()
    repository = InMemoryLocalNodeRepository(purpose="test")
    missing = wire_local_node_control_plane(
        app,
        enabled=True,
        environment="test",
        repository=repository,
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=None,
    )
    assert missing.enabled is False
    assert app.state.local_node_control_service is None

    rejected = wire_local_node_control_plane(
        app,
        enabled=True,
        environment="production",
        repository=repository,
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=_Delivery(),
    )
    assert rejected.reason == "non_durable_repository_rejected"
    assert app.state.local_node_dispatch_authority is None

    non_durable = SimpleNamespace(durable_dispatch_fence=False)
    rejected_fence = wire_local_node_control_plane(
        app,
        enabled=True,
        environment="production",
        repository=non_durable,  # type: ignore[arg-type]
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=_Delivery(),
    )
    assert rejected_fence.reason == "durable_dispatch_fence_required"
    assert app.state.local_node_control_service is None

    unsafe_provider = SimpleNamespace(idempotent_enqueue=False)
    rejected_provider = wire_local_node_control_plane(
        app,
        enabled=True,
        environment="test",
        repository=repository,
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=unsafe_provider,  # type: ignore[arg-type]
    )
    assert rejected_provider.reason == "idempotent_delivery_required"
    assert app.state.local_node_control_service is None

    enabled = wire_local_node_control_plane(
        app,
        enabled=True,
        environment="test",
        repository=repository,
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=_Delivery(),
    )
    assert enabled.enabled is True
    assert isinstance(app.state.local_node_control_service, LocalNodeControlPlaneService)

    with pytest.raises(ValueError, match="restricted to development/test"):
        InMemoryLocalNodeRepository(purpose="production")  # type: ignore[arg-type]
