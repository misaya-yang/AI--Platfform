"""Restart and delivery-fence tests for the explicit local SQLite repository."""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.api.routes.local_nodes import LocalNodeServiceFault
from assistant_service.core.local_node import (
    LocalNodeControlPlaneService,
    SQLiteLocalNodeRepository,
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
        return f"{prefix}-sqlite-{self.value}"


class _DurableDelivery:
    idempotent_enqueue = True

    def __init__(self, *, lose_first_ack: bool = False) -> None:
        self.accepted: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self._lose_first_ack = lose_first_ack

    async def enqueue_action(self, **values: Any) -> str:
        self.calls.append(values)
        action_id = values["action_id"]
        digest = values["envelope_digest"]
        existing = self.accepted.setdefault(action_id, digest)
        if existing != digest:
            raise RuntimeError("provider action digest conflict")
        if self._lose_first_ack:
            self._lose_first_ack = False
            raise RuntimeError("provider accepted action but acknowledgement was lost")
        return f"delivery-{action_id}"

    async def cancel_action(self, **values: Any) -> None:
        del values


def _channel() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel_id="channel-a",
    )


def _service(
    repository: SQLiteLocalNodeRepository,
    delivery: _DurableDelivery,
) -> LocalNodeControlPlaneService:
    return LocalNodeControlPlaneService(
        repository=repository,
        action_provider=delivery,
        id_factory=_Ids(),
        user_code_factory=lambda: "123456",
    )


async def _configure(
    service: LocalNodeControlPlaneService,
) -> tuple[SimpleNamespace, str, str]:
    challenge = await service.create_pairing_challenge(
        tenant_id="tenant-a",
        user_id="user-a",
        ttl_seconds=180,
    )
    challenge_id = challenge["challenge"]["challenge_id"]
    channel = _channel()
    await service.complete_pairing(
        tenant_id="tenant-a",
        user_id="user-a",
        challenge_id=challenge_id,
        channel=channel,
        display_name="Durable Mac",
        platform="macos",
        node_version="0.1.0",
        protocol_version=LOCAL_NODE_PROTOCOL_VERSION,
        capability_claims=["file.read"],
        permission_snapshot_digest="sha256:" + "a" * 64,
    )
    await service.record_capability_snapshot(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        revision=1,
        capabilities={"file.read": "ready"},
    )
    grant = await service.create_grant(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        kind="workspace",
        channel=channel,
        grant={
            "display_name": "Durable Workspace",
            "resource_ref": "workspace-ref",
            "capabilities": ["file.read"],
            "session_id": "session-a",
        },
    )
    return channel, grant["grant"]["grant_id"], challenge_id


def _envelope(
    grant_id: str,
    *,
    approval_id: str | None = None,
    idempotency_key: str = "idem-sqlite-a",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    arguments = {"grant_id": grant_id, "path": "notes.txt"}
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
        "expires_at": now + timedelta(minutes=8),
        "trace_context": {"traceparent": "opaque-trace-ref"},
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


def _authority(envelope: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        authority_id="gateway-authority-a",
        envelope_digest=canonical_digest(envelope),
    )


def _receipt(envelope: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=2)
    receipt = {
        "approval_id": envelope["approval_id"],
        "approved": True,
        "arguments_digest": envelope["arguments_digest"],
        "target_snapshot_digest": envelope["target_snapshot_digest"],
        "policy_snapshot_digest": envelope["policy_snapshot_digest"],
        "decision_nonce": "durable-approval-nonce-a",
        "decided_at": now,
        "expires_at": expires_at,
        "local_signature": "trusted-local-signature",
        "reason_code": None,
    }
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
            "nonce": receipt["decision_nonce"],
            "expires_at": expires_at.timestamp(),
            "local_signature": receipt["local_signature"],
        },
    }
    return receipt


@pytest.mark.asyncio
async def test_reopen_restores_device_grant_pending_approval_action_and_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "local-node-control.sqlite3"
    delivery = _DurableDelivery()
    repository = SQLiteLocalNodeRepository(database_path, purpose="test")
    assert repository.durable_dispatch_fence is True
    service = _service(repository, delivery)
    channel, grant_id, challenge_id = await _configure(service)
    envelope = _envelope(grant_id, approval_id="approval-local-a")
    pending = await service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    action_id = pending["action"]["action_id"]
    assert pending["action"]["status"] == "awaiting_approval"
    assert delivery.calls == []
    repository.close()

    reopened = SQLiteLocalNodeRepository(database_path, purpose="test")
    service = _service(reopened, delivery)
    devices = await service.list_devices(tenant_id="tenant-a", user_id="user-a")
    grants = await service.list_grants(tenant_id="tenant-a", user_id="user-a", device_id="device-a")
    restored = await service.get_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id=action_id,
    )
    assert devices["devices"][0]["display_name"] == "Durable Mac"
    assert grants["grants"][0]["grant_id"] == grant_id
    assert restored["action"]["status"] == "awaiting_approval"
    with pytest.raises(LocalNodeServiceFault) as pairing_replay:
        await service.complete_pairing(
            tenant_id="tenant-a",
            user_id="user-a",
            challenge_id=challenge_id,
            channel=channel,
            display_name="Replay",
            platform="macos",
            node_version="0.1.0",
            protocol_version=LOCAL_NODE_PROTOCOL_VERSION,
            capability_claims=["file.read"],
            permission_snapshot_digest="sha256:" + "a" * 64,
        )
    assert pairing_replay.value.code == "LOCAL_NODE_PAIRING_REPLAYED"

    approved = await service.record_approval_receipt(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id=action_id,
        channel=channel,
        receipt=_receipt(envelope),
    )
    assert approved["action_status"] == "dispatched"
    assert len(delivery.calls) == 1
    occurred_at = datetime.now(timezone.utc)
    await service.append_events(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        channel=channel,
        events=[
            {
                "event_id": "event-observed",
                "sequence": 1,
                "event_type": "action.observed",
                "occurred_at": occurred_at,
                "action_id": action_id,
                "status": "observed",
                "artifact_refs": [
                    "artifact:file-read-a",
                    "artifact:file-search-a",
                    "artifact:file-watch-a",
                ],
            },
            {
                "event_id": "event-succeeded",
                "sequence": 2,
                "event_type": "action.succeeded",
                "occurred_at": occurred_at,
                "action_id": action_id,
                "status": "succeeded",
                "result_digest": "sha256:" + "d" * 64,
                "artifact_refs": ["artifact-result-a"],
            },
        ],
    )
    reopened.close()

    final_repository = SQLiteLocalNodeRepository(database_path, purpose="test")
    final_service = _service(final_repository, delivery)
    action = await final_service.get_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id=action_id,
    )
    events = await final_service.list_events(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        after_sequence=0,
        limit=100,
    )
    assert action["action"]["status"] == "succeeded"
    assert action["action"]["observation_ref"] == "artifact:file-read-a"
    assert action["action"]["artifact_refs"] == ["artifact-result-a"]
    assert [event["sequence"] for event in events["events"]] == [1, 2]
    assert events["events"][0]["artifact_refs"] == [
        "artifact:file-read-a",
        "artifact:file-search-a",
        "artifact:file-watch-a",
    ]
    assert events["events"][1]["artifact_refs"] == ["artifact-result-a"]
    with pytest.raises(LocalNodeServiceFault) as approval_replay:
        await final_service.record_approval_receipt(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            action_id=action_id,
            channel=channel,
            receipt=_receipt(envelope),
        )
    assert approval_replay.value.code == "LOCAL_NODE_APPROVAL_REPLAYED"
    final_repository.close()


@pytest.mark.asyncio
async def test_restart_retries_same_fence_after_provider_accepts_but_ack_is_lost(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "local-node-delivery.sqlite3"
    delivery = _DurableDelivery(lose_first_ack=True)
    repository = SQLiteLocalNodeRepository(database_path, purpose="test")
    service = _service(repository, delivery)
    _, grant_id, _ = await _configure(service)
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
    assert len(delivery.accepted) == 1
    action_id = next(iter(delivery.accepted))
    repository.close()

    reopened = SQLiteLocalNodeRepository(database_path, purpose="test")
    service = _service(reopened, delivery)
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
    assert len(delivery.accepted) == 1
    assert {call["action_id"] for call in delivery.calls} == {action_id}
    assert {call["envelope_digest"] for call in delivery.calls} == {canonical_digest(envelope)}
    reopened.close()

    final_repository = SQLiteLocalNodeRepository(database_path, purpose="test")
    final_service = _service(final_repository, delivery)
    repeated = await final_service.dispatch_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        envelope=envelope,
        dispatch_authority=_authority(envelope),
    )
    assert repeated["action"]["status"] == "dispatched"
    assert len(delivery.calls) == 2

    changed = {**envelope, "call_id": "call-mutated-after-restart"}
    with pytest.raises(LocalNodeServiceFault) as conflict:
        await final_service.dispatch_action(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            envelope=changed,
            dispatch_authority=_authority(changed),
        )
    assert conflict.value.code == "LOCAL_NODE_IDEMPOTENCY_CONFLICT"
    assert len(delivery.calls) == 2
    final_repository.close()


def test_sqlite_repository_is_private_explicit_and_not_production_wired(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "local-node-private.sqlite3"
    repository = SQLiteLocalNodeRepository(database_path, purpose="test")
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600

    app = FastAPI()
    disabled = wire_local_node_control_plane(
        app,
        enabled=False,
        environment="test",
        repository=repository,
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=_DurableDelivery(),
    )
    assert disabled.reason == "disabled"
    assert app.state.local_node_control_service is None

    unsupported = wire_local_node_control_plane(
        app,
        enabled=True,
        environment="production",
        repository=repository,
        channel_verifier=object(),
        dispatch_authority=object(),
        action_provider=_DurableDelivery(),
    )
    assert unsupported.reason == "repository_environment_unsupported"
    assert app.state.local_node_control_service is None
    repository.close()
    assert repository.durable_dispatch_fence is False

    with pytest.raises(ValueError, match="absolute"):
        SQLiteLocalNodeRepository(Path("relative.sqlite3"), purpose="test")
