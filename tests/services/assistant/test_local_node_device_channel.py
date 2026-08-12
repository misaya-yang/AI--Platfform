"""Reachable ASGI proof for owner challenge -> outbound device channel."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from assistant_service.api.routes.local_node_device_channel import router
from assistant_service.core.local_node.control_plane import (
    InMemoryLocalNodeRepository,
    LocalNodeControlPlaneService,
)
from assistant_service.core.local_node.device_channel import (
    PROTOCOL_VERSION,
    SQLiteDeviceChannelBroker,
    pairing_proof_payload,
    pairing_redemption_digest,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _Delivery:
    idempotent_enqueue = True

    async def enqueue_action(self, **values: Any) -> str:
        return "delivery-" + values["action_id"]

    async def cancel_action(self, **values: Any) -> None:
        del values
        return None


class _Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return "device-e2e" if prefix == "node" else f"{prefix}-{self.value}"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _setup(tmp_path: Path):
    ids = _Ids()
    service = LocalNodeControlPlaneService(
        repository=InMemoryLocalNodeRepository(purpose="test"),
        action_provider=_Delivery(),
        id_factory=ids,
        user_code_factory=lambda: "123456",
    )
    broker = SQLiteDeviceChannelBroker(
        tmp_path / "device-channel.sqlite",
        control_service=service,
    )
    service.set_pairing_challenge_observer(broker.register_challenge)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/assistant")
    app.state.local_node_device_channel_broker = broker
    return app, service, broker


async def _challenge(service: LocalNodeControlPlaneService) -> dict[str, Any]:
    result = await service.create_pairing_challenge(
        tenant_id="tenant-a",
        user_id="user-a",
        ttl_seconds=180,
    )
    return result["challenge"]


def _redemption(challenge: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    public_key = _b64url(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    base = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "pairing_redeem",
        "challenge_id": challenge["challenge_id"],
        "user_code": challenge["user_code"],
        "device_id": "device-e2e",
        "proof_algorithm": "ed25519",
        "proof_public_key": public_key,
        "display_name": "Local Mac",
        "platform": "macos",
        "node_version": "0.1.0",
        "capability_claims": ["file.read"],
        "permission_snapshot_digest": "a" * 64,
    }
    digest = pairing_redemption_digest(
        challenge_id=base["challenge_id"],
        user_code=base["user_code"],
        device_id=base["device_id"],
        proof_algorithm=base["proof_algorithm"],
        proof_public_key=base["proof_public_key"],
        display_name=base["display_name"],
        platform=base["platform"],
        node_version=base["node_version"],
        capability_claims=tuple(base["capability_claims"]),
        permission_snapshot_digest=base["permission_snapshot_digest"],
    )
    return {
        **base,
        "device_proof": _b64url(private_key.sign(pairing_proof_payload(digest))),
    }


@pytest.mark.asyncio
async def test_device_pairing_heartbeat_revoke_is_reachable_and_secret_safe(tmp_path: Path):
    app, service, broker = _setup(tmp_path)
    challenge = await _challenge(service)
    body = _redemption(challenge, Ed25519PrivateKey.generate())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://assistant.test") as client:
        paired = await client.post("/api/v1/assistant/local-node-device", json=body)
        assert paired.status_code == 200, paired.text
        credential = paired.json()["credential"]
        assert set(paired.json()) == {
            "protocol_version",
            "device_id",
            "credential",
            "expires_at",
        }
        assert paired.json()["protocol_version"] == PROTOCOL_VERSION

        heartbeat = await client.post(
            "/api/v1/assistant/local-node-device",
            headers={"Authorization": f"Device {credential}"},
            json={
                "protocol_version": PROTOCOL_VERSION,
                "kind": "heartbeat",
                "device_id": "device-e2e",
                "doctor": {
                    "capability_revision": 1,
                    "capabilities": {"file.read": "ready"},
                    "permissions": [
                        {
                            "permission": "files",
                            "state": "ready",
                            "reason_code": None,
                            "action_hint": None,
                        }
                    ],
                },
                "receipts": [],
                "sent_at": datetime.now(timezone.utc).timestamp(),
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert heartbeat.json() == {
            "protocol_version": PROTOCOL_VERSION,
            "accepted_through_sequence": 0,
            "commands": [],
        }
        doctor = await service.get_permission_doctor(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-e2e",
        )
        assert doctor["permissions"][0]["permission"] == "files"
        assert doctor["permissions"][0]["state"] == "ready"

        await service.revoke_device(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-e2e",
        )
        rejected = await client.post(
            "/api/v1/assistant/local-node-device",
            headers={"Authorization": f"Device {credential}"},
            json={
                "protocol_version": PROTOCOL_VERSION,
                "kind": "heartbeat",
                "device_id": "device-e2e",
                "doctor": {},
                "receipts": [],
                "sent_at": datetime.now(timezone.utc).timestamp(),
            },
        )
        assert rejected.status_code == 401
    assert broker.secret_canary_absent(challenge["user_code"], credential)


@pytest.mark.asyncio
async def test_pairing_proof_binds_metadata_and_attempt_is_single_use(tmp_path: Path):
    app, service, broker = _setup(tmp_path)
    challenge = await _challenge(service)
    body = _redemption(challenge, Ed25519PrivateKey.generate())
    body["capability_claims"] = ["file.read", "process.run"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://assistant.test") as client:
        tampered = await client.post("/api/v1/assistant/local-node-device", json=body)
        assert tampered.status_code == 403
        retry = await client.post("/api/v1/assistant/local-node-device", json=body)
        assert retry.status_code == 409
    assert broker.secret_canary_absent(challenge["user_code"])


@pytest.mark.asyncio
async def test_device_endpoint_rejects_browser_auth_and_bad_protocol(tmp_path: Path):
    app, service, _ = _setup(tmp_path)
    challenge = await _challenge(service)
    body = _redemption(challenge, Ed25519PrivateKey.generate())
    body["protocol_version"] = "1"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://assistant.test") as client:
        incompatible = await client.post("/api/v1/assistant/local-node-device", json=body)
        assert incompatible.status_code == 422
        heartbeat = await client.post(
            "/api/v1/assistant/local-node-device",
            headers={"Authorization": "Bearer browser-token"},
            json={
                "protocol_version": PROTOCOL_VERSION,
                "kind": "heartbeat",
                "device_id": "device-e2e",
                "doctor": {},
                "receipts": [],
                "sent_at": datetime.now(timezone.utc).timestamp(),
            },
        )
        assert heartbeat.status_code == 401
