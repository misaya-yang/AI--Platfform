from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api import deps
from src.api.schemas.assistant import AssistantChatRequest
from src.api.v1 import _assistant_proxy
from src.api.v1 import assistant as assistant_routes
from src.api.v1.assistant import _check_model_permission, _user_can_access_model
from src.core.auth.user_resolver import UserContext


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hello", "kb_dataset_ids": [f"dataset-{i}" for i in range(9)]},
        {"message": "hello", "kb_dataset_ids": ["dataset-a", "dataset-a"]},
        {"message": "hello", "kb_dataset_ids": ["x" * 129]},
        {"message": "hello", "kb_include_images": True},
    ],
)
def test_assistant_request_rejects_unbounded_or_multimodal_kb_scope(payload) -> None:
    with pytest.raises(ValidationError):
        AssistantChatRequest.model_validate(payload)


def test_assistant_request_rejects_unresumable_plan_confirmation() -> None:
    with pytest.raises(ValidationError, match="durable plan approval"):
        AssistantChatRequest.model_validate({"message": "hello", "confirm_plan": True})


def test_assistant_request_requires_local_node_device_and_grant_selection() -> None:
    assert AssistantChatRequest.model_validate(
        {"message": "hello", "os_agent_enabled": True}
    ).local_node_device_id is None

    with pytest.raises(ValidationError, match="selectors require one device"):
        AssistantChatRequest.model_validate(
            {"message": "hello", "local_node_device_id": "device-a"}
        )

    request = AssistantChatRequest.model_validate(
        {
            "message": "analyze authorized files",
            "os_agent_enabled": True,
            "local_node_device_id": "device-a",
            "local_node_grant_ids": ["grant-a"],
        }
    )
    assert request.local_node_device_id == "device-a"
    assert request.local_node_grant_ids == ["grant-a"]


def test_assistant_request_openapi_marks_plan_confirmation_as_false_only() -> None:
    schema = AssistantChatRequest.model_json_schema()["properties"]["confirm_plan"]

    assert schema["const"] is False
    assert schema["default"] is False


def test_streaming_gateway_rejects_plan_confirmation_before_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*args, **kwargs) -> None:
        del args, kwargs

    async def forbidden_proxy(*args, **kwargs):
        del args, kwargs
        raise AssertionError("unsupported plan confirmation reached the downstream service")

    app = FastAPI()
    app.include_router(assistant_routes.router)
    app.dependency_overrides[deps.get_user_context] = lambda: UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        is_authenticated=True,
    )
    monkeypatch.setattr(deps, "enforce_rate_limit", no_rate_limit)
    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", forbidden_proxy)

    response = TestClient(app).post(
        "/assistant/chat/stream",
        json={"message": "hello", "confirm_plan": True},
    )

    assert response.status_code == 422
    assert "confirm_plan" in response.text


@pytest.mark.parametrize("path", ["/assistant/chat", "/assistant/chat/stream"])
def test_gateway_resolves_authorizes_and_proxies_omitted_default_model(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission_lookups: list[tuple[str, str]] = []
    proxied_bodies: list[dict[str, object]] = []

    async def no_rate_limit(*args, **kwargs) -> None:
        del args, kwargs

    async def get_access_level(tenant_id: str, model_id: str) -> str:
        permission_lookups.append((tenant_id, model_id))
        return "public"

    async def proxy(_request, _user, *, path: str, body: bytes):
        payload = json.loads(body)
        proxied_bodies.append(payload)
        if path == "chat":
            return {
                "content": "ok",
                "usage": {},
                "contexts": [],
                "duration_ms": 1,
                "model_id": payload["model_id"],
                "session_id": None,
            }
        return {"proxied": True}

    app = FastAPI()
    app.state.settings = SimpleNamespace(default_model="tenant-default-model")
    app.state.model_meta = SimpleNamespace(get_access_level=get_access_level)
    app.include_router(assistant_routes.router)
    app.dependency_overrides[deps.get_user_context] = lambda: UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        is_authenticated=True,
    )
    monkeypatch.setattr(deps, "enforce_rate_limit", no_rate_limit)
    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", proxy)

    response = TestClient(app).post(path, json={"message": "hello"})

    assert response.status_code == 200, response.text
    assert permission_lookups == [("tenant-1", "tenant-default-model")]
    assert proxied_bodies == [{"message": "hello", "model_id": "tenant-default-model"}]


@pytest.mark.parametrize("path", ["/assistant/chat", "/assistant/chat/stream"])
def test_gateway_denies_omitted_default_model_before_proxy(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*args, **kwargs) -> None:
        del args, kwargs

    async def get_access_level(_tenant_id: str, model_id: str) -> str:
        assert model_id == "restricted-default-model"
        return "premium"

    async def forbidden_proxy(*args, **kwargs):
        del args, kwargs
        raise AssertionError("restricted default model reached the downstream service")

    app = FastAPI()
    app.state.settings = SimpleNamespace(default_model="restricted-default-model")
    app.state.model_meta = SimpleNamespace(get_access_level=get_access_level)
    app.include_router(assistant_routes.router)
    app.dependency_overrides[deps.get_user_context] = lambda: UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier="normal",
        is_authenticated=True,
    )
    monkeypatch.setattr(deps, "enforce_rate_limit", no_rate_limit)
    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", forbidden_proxy)

    response = TestClient(app).post(path, json={"message": "hello"})

    assert response.status_code == 403
    assert "restricted-default-model" in response.text


@pytest.mark.parametrize(
    ("tier", "roles", "access_level", "expected"),
    [
        ("normal", [], "public", True),
        ("normal", [], "premium", False),
        ("premium", [], "premium", True),
        ("normal", [], "admin", False),
        ("admin", [], "admin", True),
        ("normal", ["admin"], "admin", True),
    ],
)
def test_user_model_access_preserves_known_access_level_rules(
    tier: str,
    roles: list[str],
    access_level: str,
    expected: bool,
) -> None:
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier=tier,
        roles=roles,
        is_authenticated=True,
    )

    assert _user_can_access_model(user, access_level) is expected


@pytest.mark.parametrize("tier", ["normal", "admin"])
def test_user_model_access_rejects_unknown_access_level_for_every_tier(tier: str) -> None:
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier=tier,
        is_authenticated=True,
    )

    assert _user_can_access_model(user, "corrupt") is False


@pytest.mark.asyncio
async def test_model_permission_rejects_dirty_database_access_level() -> None:
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier="admin",
        is_authenticated=True,
    )
    model_meta = SimpleNamespace()

    async def get_access_level(_tenant_id: str, _model_id: str) -> str:
        return "corrupt"

    model_meta.get_access_level = get_access_level

    with pytest.raises(HTTPException) as exc_info:
        await _check_model_permission(user, "model-1", model_meta)

    assert exc_info.value.status_code == 403
