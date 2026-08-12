"""Durable file command/result queue invariants."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from assistant_service.core.local_node.control_plane import canonical_digest
from assistant_service.core.local_node.device_delivery import SQLiteDeviceDelivery
from local_node.models import ActionContext
from local_node.transport import action_to_wire


class _ControlSigner:
    key_id = "test-control-key"

    def sign(self, payload: bytes) -> str:
        return hashlib.sha256(b"test-control:" + payload).hexdigest()


def _envelope(*, action_id: str = "action-file-read") -> dict[str, Any]:
    arguments = {"grant_id": "grant-local", "path": "docs/notes.txt"}
    unsigned = ActionContext.create(
        action_id=action_id,
        idempotency_key="idem-file-read",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        run_id="run-a",
        agent_id="assistant",
        agent_version="builtin-assistant/v1",
        call_id="call-file-read",
        device_id="device-a",
        envelope_version=1,
        capability="file.read",
        tool_name="local_file_read",
        operation="file.read",
        capability_lease_id="grant-local",
        resource_refs=("grant-local", "docs/notes.txt"),
        normalized_arguments=arguments,
        target_snapshot_digest="target-a",
        policy_snapshot_digest="policy-a",
        nonce="nonce-file-read",
        platform_key_id="test-platform-key",
        ttl_seconds=60,
    )
    action = replace(unsigned, platform_signature="test-platform-signature")
    signed = action_to_wire(action)
    return {
        "tool_name": "local_file_read",
        "action_operation": "file.read",
        "normalized_arguments": {
            "grant_id": "grant-local",
            "model_arguments": arguments,
        },
        "signed_action": signed,
    }


@pytest.mark.asyncio
async def test_file_command_result_is_owner_bound_validated_and_one_use(tmp_path: Path) -> None:
    delivery = SQLiteDeviceDelivery(
        tmp_path / "delivery.sqlite",
        purpose="test",
        control_signer=_ControlSigner(),
    )
    envelope = _envelope()
    digest = canonical_digest(envelope)
    await delivery.enqueue_action(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id="action-file-read",
        idempotency_key="idem-file-read",
        envelope_digest=digest,
        envelope=envelope,
    )

    commands = await delivery.claim_commands(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
    )
    assert len(commands) == 1
    assert commands[0]["action"]["operation"] == "file.read"

    content = "needle in a temporary fixture\n"
    result = {
        "kind": "file_read",
        "relative_path": "docs/notes.txt",
        "content": content,
        "encoding": "utf-8",
        "size": len(content.encode()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    receipt = {
        "event_id": "event-file-read",
        "sequence": 1,
        "event_type": "action.succeeded",
        "action_id": "action-file-read",
        "status": "succeeded",
        "occurred_at": time.time(),
        "result": result,
        "result_digest": hashlib.sha256(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "error_code": None,
        "summary": None,
    }
    prepared = await delivery.prepare_result_receipts(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        receipts=[receipt],
    )
    await delivery.accept_prepared_results(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        results=prepared,
    )

    assert await delivery.await_result(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id="action-file-read",
        timeout_seconds=0.1,
    ) == result
    assert await delivery.await_result(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        action_id="action-file-read",
        timeout_seconds=0.1,
    ) is None
    assert delivery.secret_canary_absent(content)


@pytest.mark.asyncio
async def test_control_command_insert_matches_schema(tmp_path: Path) -> None:
    delivery = SQLiteDeviceDelivery(
        tmp_path / "delivery.sqlite",
        purpose="test",
        control_signer=_ControlSigner(),
    )
    command_id = await delivery.enqueue_emergency_stop(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
    )
    commands = await delivery.claim_commands(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
    )
    assert commands == ({
        **commands[0],
        "command_id": command_id,
        "kind": "emergency_stop",
    },)
