"""Gateway exposure tests for the Local Node control plane."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_user_context
from src.api.v1 import _assistant_proxy
from src.api.v1 import assistant as assistant_api
from src.core.auth.user_resolver import UserContext


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(assistant_api.router)
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        tenant_id="tenant-gateway",
        user_id="user-gateway",
        is_authenticated=True,
    )
    return TestClient(app)


def test_gateway_exposes_only_owner_safe_local_node_control_plane() -> None:
    app = FastAPI()
    app.include_router(assistant_api.router)
    operations = {
        (path, method)
        for path, definition in app.openapi()["paths"].items()
        if path.startswith("/assistant/local-nodes")
        for method in definition
        if method in {"get", "post", "delete"}
    }

    assert operations == {
        ("/assistant/local-nodes/pairing/challenges", "post"),
        ("/assistant/local-nodes", "get"),
        ("/assistant/local-nodes/{device_id}/revoke", "post"),
        ("/assistant/local-nodes/{device_id}/status", "get"),
        ("/assistant/local-nodes/{device_id}/capabilities", "get"),
        ("/assistant/local-nodes/{device_id}/doctor", "get"),
        ("/assistant/local-nodes/{device_id}/grants", "get"),
        ("/assistant/local-nodes/{device_id}/grants/{grant_id}", "delete"),
        ("/assistant/local-nodes/{device_id}/events", "get"),
    }

    # In particular, Web auth cannot reach these trusted/device/action-plane
    # operations through the Gateway.
    forbidden = {
        ("/assistant/local-nodes/pairing/challenges/{challenge_id}/complete", "post"),
        ("/assistant/local-nodes/{device_id}/grants/workspaces", "post"),
        ("/assistant/local-nodes/{device_id}/grants/apps", "post"),
        ("/assistant/local-nodes/{device_id}/grants/domains", "post"),
        ("/assistant/local-nodes/{device_id}/actions", "post"),
        ("/assistant/local-nodes/{device_id}/actions/{action_id}", "get"),
        ("/assistant/local-nodes/{device_id}/actions/{action_id}/cancel", "post"),
        (
            "/assistant/local-nodes/{device_id}/actions/{action_id}/approval-receipts",
            "post",
        ),
        ("/assistant/local-nodes/{device_id}/events", "post"),
    }
    assert operations.isdisjoint(forbidden)


def test_gateway_proxy_uses_authenticated_owner_and_narrow_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []

    async def proxy(request, user, *, path: str, body: bytes | None = None):
        observed.append(
            {
                "path": path,
                "tenant_id": user.tenant_id,
                "user_id": user.user_id,
                "body": body,
                "query": request.url.query,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", proxy)
    client = _client()

    forged = client.post(
        "/assistant/local-nodes/pairing/challenges",
        json={"tenant_id": "tenant-forged", "ttl_seconds": 180},
    )
    assert forged.status_code == 422
    assert observed == []

    challenge = client.post(
        "/assistant/local-nodes/pairing/challenges",
        json={"display_name_hint": "My Mac", "ttl_seconds": 180},
    )
    events = client.get("/assistant/local-nodes/device-a/events?after_sequence=7&limit=20")

    assert challenge.status_code == 201
    assert events.status_code == 200
    assert observed[0]["path"] == "local-nodes/pairing/challenges"
    assert json.loads(observed[0]["body"]) == {
        "display_name_hint": "My Mac",
        "ttl_seconds": 180,
    }
    assert observed[1]["path"] == "local-nodes/device-a/events"
    assert observed[1]["query"] == "after_sequence=7&limit=20"
    assert {(entry["tenant_id"], entry["user_id"]) for entry in observed} == {
        ("tenant-gateway", "user-gateway")
    }


@pytest.mark.parametrize(
    "path",
    [
        "/assistant/local-nodes/%2Fetc/status",
        "/assistant/local-nodes/%2E%2E/status",
        "/assistant/local-nodes/device%3Fadmin/status",
        "/assistant/local-nodes/device-a/grants/grant%2Fother",
        "/assistant/local-nodes/device-a/grants/grant%3Fforce",
        "/assistant/local-nodes/" + "a" * 129 + "/status",
    ],
)
def test_gateway_rejects_path_like_opaque_ids_before_proxy(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_proxy(*args, **kwargs):
        raise AssertionError("invalid opaque ID reached the upstream proxy")

    monkeypatch.setattr(
        _assistant_proxy,
        "proxy_to_assistant_service",
        forbidden_proxy,
    )

    response = _client().get(path)

    assert response.status_code in {404, 405, 422}
