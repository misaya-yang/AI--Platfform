"""Focused contract tests for the Local Node control-plane router."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from assistant_service.api.routes import local_nodes
from assistant_service.auth import UserContext, get_user_context
from fastapi import FastAPI
from fastapi.testclient import TestClient

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class _Service:
    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        async def invoke(**values: Any) -> Any:
            self.calls.append((name, values))
            result = self.results.get(name)
            if isinstance(result, Exception):
                raise result
            return result

        return invoke


class _Verifier:
    def __init__(
        self,
        *,
        tenant_id: str = "tenant-a",
        user_id: str = "user-a",
        device_id: str | None = "device-a",
    ) -> None:
        self.principal = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "device_id": device_id,
            "channel_id": "channel-a",
            "private_attestation": "must-not-leak",
        }
        self.calls: list[dict[str, Any]] = []

    async def verify(self, **values: Any) -> dict[str, Any]:
        self.calls.append(values)
        return self.principal


class _DispatchAuthority:
    def __init__(
        self,
        *,
        tenant_id: str = "tenant-a",
        user_id: str = "user-a",
        device_id: str = "device-a",
        override_digest: str | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.device_id = device_id
        self.override_digest = override_digest
        self.calls: list[dict[str, Any]] = []

    async def authorize(self, **values: Any) -> dict[str, Any]:
        self.calls.append(values)
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "authority_id": "execution-gateway-receipt-a",
            "envelope_digest": self.override_digest or values["envelope_digest"],
        }


def _client(
    service: _Service | None = None,
    *,
    verifier: _Verifier | None = None,
    dispatch_authority: _DispatchAuthority | None = None,
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
) -> TestClient:
    app = FastAPI()
    app.include_router(local_nodes.router)
    if service is not None:
        app.state.local_node_control_service = service
    if verifier is not None:
        app.state.local_node_channel_verifier = verifier
    if dispatch_authority is not None:
        app.state.local_node_dispatch_authority = dispatch_authority
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        tenant_id=tenant_id,
        user_id=user_id,
    )
    return TestClient(app)


def _device() -> dict[str, Any]:
    return {
        "device_id": "device-a",
        "display_name": "Yang's Mac",
        "platform": "macos",
        "node_version": "0.1.0",
        "status": "online",
        "last_seen_at": _iso(),
        "credential": "must-not-leak",
    }


def _action(status: str = "proposed") -> dict[str, Any]:
    return {
        "action_id": "action-a",
        "device_id": "device-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "call_id": "call-a",
        "capability": "file.read",
        "status": status,
        "sequence": 1,
        "created_at": _iso(),
        "updated_at": _iso(),
        "artifact_refs": [],
        "normalized_arguments": {"secret": "must-not-leak"},
    }


def _action_request() -> dict[str, Any]:
    issued_at = _now()
    return {
        "idempotency_key": "idem-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "call_id": "call-a",
        "capability": "file.read",
        "normalized_arguments": {"resource_ref": "workspace-a", "relative_path": "README.md"},
        "arguments_digest": _DIGEST_A,
        "target_snapshot_digest": _DIGEST_B,
        "policy_snapshot_digest": _DIGEST_C,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(minutes=2)).isoformat(),
        "trace_context": {"traceparent": "00-abc-def-01"},
    }


def _approval_request() -> dict[str, Any]:
    decided_at = _now()
    return {
        "approval_id": "approval-a",
        "approved": True,
        "arguments_digest": _DIGEST_A,
        "target_snapshot_digest": _DIGEST_B,
        "policy_snapshot_digest": _DIGEST_C,
        "decision_nonce": "nonce-a",
        "decided_at": decided_at.isoformat(),
        "expires_at": (decided_at + timedelta(minutes=2)).isoformat(),
    }


def test_router_exposes_the_complete_control_plane_contract() -> None:
    app = FastAPI()
    app.include_router(local_nodes.router)
    operations = {
        (path, method)
        for path, definition in app.openapi()["paths"].items()
        for method in definition
        if method in {"get", "post", "delete"}
    }

    assert operations == {
        ("/local-nodes/pairing/challenges", "post"),
        ("/local-nodes/pairing/challenges/{challenge_id}/complete", "post"),
        ("/local-nodes", "get"),
        ("/local-nodes/{device_id}/revoke", "post"),
        ("/local-nodes/{device_id}/status", "get"),
        ("/local-nodes/{device_id}/capabilities", "get"),
        ("/local-nodes/{device_id}/doctor", "get"),
        ("/local-nodes/{device_id}/grants/workspaces", "post"),
        ("/local-nodes/{device_id}/grants/apps", "post"),
        ("/local-nodes/{device_id}/grants/domains", "post"),
        ("/local-nodes/{device_id}/grants", "get"),
        ("/local-nodes/{device_id}/grants/{grant_id}", "delete"),
        ("/local-nodes/{device_id}/actions", "post"),
        ("/local-nodes/{device_id}/actions/{action_id}", "get"),
        ("/local-nodes/{device_id}/actions/{action_id}/cancel", "post"),
        (
            "/local-nodes/{device_id}/actions/{action_id}/approval-receipts",
            "post",
        ),
        ("/local-nodes/{device_id}/events", "post"),
        ("/local-nodes/{device_id}/events", "get"),
    }


def test_pairing_challenge_is_owner_bound_and_filters_credentials() -> None:
    service = _Service(
        create_pairing_challenge={
            "challenge": {
                "challenge_id": "challenge-a",
                "user_code": "ABCD-1234",
                "expires_at": _iso(_now() + timedelta(minutes=3)),
                "challenge_secret": "must-not-leak",
            },
            "device_credential": "must-not-leak",
        }
    )
    client = _client(service)

    response = client.post(
        "/local-nodes/pairing/challenges",
        json={"display_name_hint": "My Mac", "ttl_seconds": 180},
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "challenge": {
            "challenge_id": "challenge-a",
            "user_code": "ABCD-1234",
            "expires_at": response.json()["challenge"]["expires_at"],
        }
    }
    assert service.calls[0][1]["tenant_id"] == "tenant-a"
    assert service.calls[0][1]["user_id"] == "user-a"
    assert (
        client.post(
            "/local-nodes/pairing/challenges",
            json={"tenant_id": "other", "ttl_seconds": 180},
        ).status_code
        == 422
    )


def test_pairing_completion_requires_independent_challenge_channel() -> None:
    service = _Service(complete_pairing={"device": _device()})
    payload = {
        "display_name": "Yang's Mac",
        "platform": "macos",
        "node_version": "0.1.0",
        "protocol_version": "1",
        "capability_claims": ["file.read", "file.watch"],
        "permission_snapshot_digest": _DIGEST_A,
    }

    unsigned = _client(service).post(
        "/local-nodes/pairing/challenges/challenge-a/complete",
        json=payload,
    )
    assert unsigned.status_code == 503
    assert unsigned.json()["detail"]["code"] == "LOCAL_NODE_CHANNEL_UNAVAILABLE"
    assert service.calls == []

    mismatched = _client(
        service,
        verifier=_Verifier(tenant_id="tenant-other", device_id=None),
    ).post(
        "/local-nodes/pairing/challenges/challenge-a/complete",
        json=payload,
    )
    assert mismatched.status_code == 403
    assert mismatched.json()["detail"]["code"] == "LOCAL_NODE_CHANNEL_OWNER_MISMATCH"
    assert service.calls == []

    verifier = _Verifier(device_id=None)
    paired = _client(service, verifier=verifier).post(
        "/local-nodes/pairing/challenges/challenge-a/complete",
        json=payload,
    )
    assert paired.status_code == 201, paired.text
    assert paired.json()["device"]["device_id"] == "device-a"
    assert "credential" not in paired.json()["device"]
    assert verifier.calls[0]["purpose"] == "pairing.complete"
    assert verifier.calls[0]["challenge_id"] == "challenge-a"
    assert verifier.calls[0]["body_digest"].startswith("sha256:")


def test_device_inventory_status_capabilities_and_doctor_are_sanitized() -> None:
    service = _Service(
        list_devices={"devices": [_device()]},
        get_device_status={
            "device": {
                "device_id": "device-a",
                "status": "online",
                "last_seen_at": _iso(),
                "active_action_id": None,
                "active_lease_expires_at": None,
                "protocol_compatible": True,
                "channel_token": "must-not-leak",
            }
        },
        get_device_capabilities={
            "device_id": "device-a",
            "revision": 7,
            "capabilities": [{"name": "file.read", "state": "ready", "secret": "must-not-leak"}],
        },
        get_permission_doctor={
            "device_id": "device-a",
            "checked_at": _iso(),
            "permissions": [
                {
                    "permission": "screen_recording",
                    "state": "needs_action",
                    "checked_at": _iso(),
                    "action_hint": "Enable Screen Recording locally",
                }
            ],
        },
    )
    client = _client(service)

    assert client.get("/local-nodes").status_code == 200
    status_response = client.get("/local-nodes/device-a/status")
    capabilities = client.get("/local-nodes/device-a/capabilities")
    doctor = client.get("/local-nodes/device-a/doctor")

    assert status_response.status_code == capabilities.status_code == doctor.status_code == 200
    assert "channel_token" not in status_response.json()["device"]
    assert "secret" not in capabilities.json()["capabilities"][0]
    assert doctor.json()["permissions"][0]["state"] == "needs_action"
    for _, call in service.calls:
        assert call["tenant_id"] == "tenant-a"
        assert call["user_id"] == "user-a"


def test_grants_require_device_channel_and_reject_raw_paths_or_wrong_capabilities() -> None:
    grant = {
        "grant": {
            "grant_id": "grant-a",
            "device_id": "device-a",
            "kind": "workspace",
            "display_name": "Project",
            "resource_ref": "workspace-a",
            "capabilities": ["file.read", "file.watch"],
            "status": "active",
            "created_at": _iso(),
            "local_path": "/Users/yang/project",
        }
    }
    service = _Service(create_grant=grant)
    payload = {
        "display_name": "Project",
        "resource_ref": "workspace-a",
        "capabilities": ["file.read", "file.watch"],
    }

    unsigned = _client(service).post(
        "/local-nodes/device-a/grants/workspaces",
        json=payload,
    )
    assert unsigned.status_code == 503
    assert service.calls == []

    client = _client(service, verifier=_Verifier())
    assert (
        client.post(
            "/local-nodes/device-a/grants/workspaces",
            json={**payload, "resource_ref": "/Users/yang/project"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/local-nodes/device-a/grants/workspaces",
            json={**payload, "capabilities": ["network.upload"]},
        ).status_code
        == 422
    )

    response = client.post(
        "/local-nodes/device-a/grants/workspaces",
        json=payload,
    )
    assert response.status_code == 201, response.text
    assert response.json()["grant"]["resource_ref"] == "workspace-a"
    assert "local_path" not in response.json()["grant"]
    assert service.calls[0][1]["device_id"] == "device-a"
    assert service.calls[0][1]["channel"].device_id == "device-a"


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        (
            {
                "display_name": "Docs",
                "domain": "EXAMPLE.COM.",
                "capabilities": ["network.fetch"],
            },
            201,
        ),
        (
            {
                "display_name": "Wildcard",
                "domain": "*.example.com",
                "capabilities": ["network.fetch"],
            },
            422,
        ),
        (
            {
                "display_name": "Wrong capability",
                "domain": "example.com",
                "capabilities": ["file.read"],
            },
            422,
        ),
    ],
)
def test_domain_grant_is_canonical_and_narrow(
    payload: dict[str, Any],
    expected_status: int,
) -> None:
    service = _Service(
        create_grant={
            "grant": {
                "grant_id": "grant-domain-a",
                "device_id": "device-a",
                "kind": "domain",
                "display_name": "Docs",
                "domain": "example.com",
                "capabilities": ["network.fetch"],
                "status": "active",
                "created_at": _iso(),
            }
        }
    )
    response = _client(service, verifier=_Verifier()).post(
        "/local-nodes/device-a/grants/domains",
        json=payload,
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        assert service.calls[0][1]["grant"]["domain"] == "example.com"


def test_action_dispatch_uses_auth_binding_and_never_echoes_arguments() -> None:
    service = _Service(dispatch_action={"action": _action()})
    authority = _DispatchAuthority()
    client = _client(service, dispatch_authority=authority)
    payload = _action_request()

    forged = client.post(
        "/local-nodes/device-a/actions",
        json={**payload, "tenant_id": "tenant-other", "device_id": "device-other"},
    )
    assert forged.status_code == 422
    assert service.calls == []

    response = client.post("/local-nodes/device-a/actions", json=payload)
    assert response.status_code == 202, response.text
    assert "normalized_arguments" not in response.json()["action"]
    values = service.calls[0][1]
    assert values["tenant_id"] == "tenant-a"
    assert values["user_id"] == "user-a"
    assert values["device_id"] == "device-a"
    assert "tenant_id" not in values["envelope"]
    assert "device_id" not in values["envelope"]
    assert values["dispatch_authority"].authority_id == "execution-gateway-receipt-a"
    assert authority.calls[0]["purpose"] == "action.dispatch"
    assert authority.calls[0]["tenant_id"] == "tenant-a"
    assert authority.calls[0]["user_id"] == "user-a"
    assert authority.calls[0]["device_id"] == "device-a"
    assert authority.calls[0]["envelope_digest"].startswith("sha256:")


def test_action_dispatch_requires_exact_execution_gateway_authority() -> None:
    service = _Service(dispatch_action={"action": _action()})
    payload = _action_request()

    # Web authentication and the ordinary Gateway transport signature are not
    # a canonical ExecutionGateway dispatch receipt.
    unsigned = _client(service).post(
        "/local-nodes/device-a/actions",
        json=payload,
        headers={"X-Gateway-Secret": "transport-auth-is-not-dispatch-authority"},
    )
    assert unsigned.status_code == 503
    assert unsigned.json()["detail"]["code"] == ("LOCAL_NODE_DISPATCH_AUTHORITY_UNAVAILABLE")
    assert service.calls == []

    mismatched = _client(
        service,
        dispatch_authority=_DispatchAuthority(device_id="device-other"),
    ).post("/local-nodes/device-a/actions", json=payload)
    assert mismatched.status_code == 403
    assert mismatched.json()["detail"]["code"] == ("LOCAL_NODE_DISPATCH_AUTHORITY_MISMATCH")
    assert service.calls == []

    wrong_digest = _client(
        service,
        dispatch_authority=_DispatchAuthority(override_digest=_DIGEST_A),
    ).post("/local-nodes/device-a/actions", json=payload)
    assert wrong_digest.status_code == 403
    assert service.calls == []


def test_action_rejects_expired_or_overlong_envelopes_before_dispatch() -> None:
    service = _Service(dispatch_action={"action": _action()})
    client = _client(service, dispatch_authority=_DispatchAuthority())
    expired = _action_request()
    issued_at = _now() - timedelta(minutes=2)
    expired["issued_at"] = issued_at.isoformat()
    expired["expires_at"] = (issued_at + timedelta(minutes=1)).isoformat()
    assert client.post("/local-nodes/device-a/actions", json=expired).status_code == 422

    overlong = _action_request()
    issued_at = _now()
    overlong["issued_at"] = issued_at.isoformat()
    overlong["expires_at"] = (issued_at + timedelta(minutes=11)).isoformat()
    assert client.post("/local-nodes/device-a/actions", json=overlong).status_code == 422
    assert service.calls == []


def test_local_approval_receipt_cannot_be_forged_by_web_authentication() -> None:
    service = _Service(
        record_approval_receipt={
            "action_id": "action-a",
            "approval_id": "approval-a",
            "recorded": True,
            "action_status": "dispatched",
            "signature": "must-not-leak",
        }
    )
    path = "/local-nodes/device-a/actions/action-a/approval-receipts"
    payload = _approval_request()

    unsigned = _client(service).post(path, json=payload)
    assert unsigned.status_code == 503
    assert service.calls == []

    wrong_device = _client(service, verifier=_Verifier(device_id="device-other")).post(
        path,
        json=payload,
    )
    assert wrong_device.status_code == 403
    assert wrong_device.json()["detail"]["code"] == "LOCAL_NODE_CHANNEL_DEVICE_MISMATCH"
    assert service.calls == []

    verifier = _Verifier()
    response = _client(service, verifier=verifier).post(path, json=payload)
    assert response.status_code == 202, response.text
    assert "signature" not in response.json()
    assert verifier.calls[0]["purpose"] == "action.approval_receipt"
    assert service.calls[0][1]["channel"].channel_id == "channel-a"


def test_event_append_requires_device_channel_and_contiguous_order() -> None:
    service = _Service(
        append_events={
            "device_id": "device-a",
            "accepted_through_sequence": 2,
            "next_expected_sequence": 3,
            "duplicate_count": 0,
            "ledger_secret": "must-not-leak",
        }
    )
    event = {
        "event_id": "event-a",
        "sequence": 1,
        "event_type": "action.running",
        "occurred_at": _iso(),
        "action_id": "action-a",
        "status": "running",
    }
    invalid = {"events": [event, {**event, "event_id": "event-b", "sequence": 3}]}
    assert (
        _client(service, verifier=_Verifier())
        .post(
            "/local-nodes/device-a/events",
            json=invalid,
        )
        .status_code
        == 422
    )
    assert service.calls == []

    batch = {"events": [event, {**event, "event_id": "event-b", "sequence": 2}]}
    unsigned = _client(service).post("/local-nodes/device-a/events", json=batch)
    assert unsigned.status_code == 503
    assert service.calls == []

    response = _client(service, verifier=_Verifier()).post(
        "/local-nodes/device-a/events",
        json=batch,
    )
    assert response.status_code == 202, response.text
    assert "ledger_secret" not in response.json()
    assert [item["sequence"] for item in service.calls[0][1]["events"]] == [1, 2]


def test_event_read_rejects_non_monotonic_service_projection() -> None:
    event = {
        "event_id": "event-a",
        "device_id": "device-a",
        "sequence": 2,
        "event_type": "action.running",
        "occurred_at": _iso(),
        "action_id": "action-a",
        "status": "running",
    }
    service = _Service(
        list_events={
            "device_id": "device-a",
            "after_sequence": 0,
            "next_sequence": 3,
            "events": [event, {**event, "event_id": "event-b", "sequence": 1}],
        }
    )

    response = _client(service).get("/local-nodes/device-a/events?after_sequence=0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LOCAL_NODE_EVENT_ORDER_INVALID"


def test_service_faults_are_stable_and_service_absence_fails_closed() -> None:
    unavailable = _client().get("/local-nodes")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "LOCAL_NODE_CONTROL_UNAVAILABLE"

    service = _Service(
        list_devices=local_nodes.LocalNodeServiceFault(
            status_code=404,
            code="LOCAL_NODE_NOT_FOUND",
        )
    )
    missing = _client(service).get("/local-nodes")
    assert missing.status_code == 404
    assert missing.json()["detail"] == {
        "code": "LOCAL_NODE_NOT_FOUND",
        "message": "Local node resource was not found",
        "request_id": "local-node-request",
    }


def test_assistant_service_composition_root_registers_fail_closed_routes() -> None:
    from assistant_service.api.router import router as assistant_api_router

    app = FastAPI()
    app.include_router(assistant_api_router, prefix="/api/v1/assistant")
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    client = TestClient(app)

    inventory = client.get("/api/v1/assistant/local-nodes")
    challenge = client.post(
        "/api/v1/assistant/local-nodes/pairing/challenges",
        json={"ttl_seconds": 180},
    )

    assert inventory.status_code == 503
    assert challenge.status_code == 503
    assert inventory.json()["detail"]["code"] == "LOCAL_NODE_CONTROL_UNAVAILABLE"
    assert challenge.json()["detail"]["code"] == "LOCAL_NODE_CONTROL_UNAVAILABLE"
