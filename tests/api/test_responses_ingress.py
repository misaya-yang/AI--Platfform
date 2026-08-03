"""Gateway boundary tests for the public ``POST /v1/responses`` alias."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.v1 import responses as responses_route
from src.core.auth.user_resolver import UserContext


class _ModelMeta:
    async def get_access_level(self, tenant_id: str, model_id: str) -> str | None:
        assert tenant_id == "tenant-1"
        return "public" if model_id == "qwen3.7-plus" else None


def _user(*, authenticated: bool = True, tenant_id: str = "tenant-1") -> UserContext:
    return UserContext(
        user_id="user-1",
        tenant_id=tenant_id,
        tier="normal",
        roles=["user"],
        is_authenticated=authenticated,
    )


def _app(user: UserContext) -> FastAPI:
    app = FastAPI()
    app.include_router(responses_route.router, prefix="/v1")
    app.state.model_meta = _ModelMeta()
    app.state.multi_rate_limiter = None
    app.dependency_overrides[responses_route.get_user_context] = lambda: user
    return app


def test_public_responses_requires_authenticated_tenant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_proxy(*_args: Any, **_kwargs: Any):
        raise AssertionError("unauthenticated request must not be proxied")

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", should_not_proxy)

    with TestClient(_app(_user(authenticated=False))) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_public_responses_accepts_authenticated_default_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.responses import JSONResponse

    captured: dict[str, Any] = {}

    async def response_proxy(request, user, *, path: str, body: bytes):
        captured.update(user=user, path=path, body=body)
        return JSONResponse({"ok": True})

    class DefaultModelMeta:
        async def get_access_level(self, tenant_id: str, model_id: str) -> str | None:
            assert tenant_id == "default"
            assert model_id == "qwen3.7-plus"
            return "public"

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", response_proxy)
    app = _app(_user(tenant_id="default"))
    app.state.model_meta = DefaultModelMeta()

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 200
    assert captured["user"].tenant_id == "default"
    assert captured["path"] == "responses"


def test_public_responses_rejects_public_tenant_even_when_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_proxy(*_args: Any, **_kwargs: Any):
        raise AssertionError("public tenant must not reach assistant-service")

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", should_not_proxy)

    with TestClient(_app(_user(tenant_id="public"))) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_public_responses_proxies_exact_body_and_preserves_idempotency_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_proxy(request, user, *, path: str, body: bytes):
        captured.update(request=request, user=user, path=path, body=body)
        return SimpleNamespace(
            status_code=200,
            headers={},
            body=b"",
            __call__=lambda *_args, **_kwargs: None,
        )

    # A real Starlette Response keeps this an HTTP-layer contract test.
    from starlette.responses import JSONResponse

    async def response_proxy(request, user, *, path: str, body: bytes):
        captured.update(request=request, user=user, path=path, body=body)
        return JSONResponse({"ok": True})

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", response_proxy)
    payload = {"model": "qwen3.7-plus", "input": "hello", "store": False}

    with TestClient(_app(_user())) as client:
        response = client.post(
            "/v1/responses",
            json=payload,
            headers={"Idempotency-Key": "idem-1"},
        )

    assert response.status_code == 200
    assert captured["path"] == "responses"
    assert json_bytes(captured["body"]) == payload
    assert captured["request"].headers["idempotency-key"] == "idem-1"
    assert captured["user"].tenant_id == "tenant-1"


def test_public_responses_checks_model_permission_before_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_proxy(*_args: Any, **_kwargs: Any):
        raise AssertionError("unknown model must not be proxied")

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", should_not_proxy)

    with TestClient(_app(_user())) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "unknown-model", "input": "hello"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_found"


def test_public_responses_fails_closed_when_model_authorizer_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_proxy(*_args: Any, **_kwargs: Any):
        raise AssertionError("request must not bypass unavailable model authorization")

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", should_not_proxy)
    app = _app(_user())
    app.state.model_meta = None

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "message": "Model authorization is temporarily unavailable.",
        "type": "server_error",
        "param": "model",
        "code": "model_authorization_unavailable",
    }


def test_public_responses_fails_closed_when_model_authorizer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenModelMeta:
        async def get_access_level(self, _tenant_id: str, _model_id: str) -> str | None:
            raise RuntimeError("database connection contains private diagnostics")

    async def should_not_proxy(*_args: Any, **_kwargs: Any):
        raise AssertionError("request must not bypass a failed model authorization query")

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", should_not_proxy)
    app = _app(_user())
    app.state.model_meta = BrokenModelMeta()

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_authorization_unavailable"
    assert "private diagnostics" not in response.text


def test_public_responses_rejects_query_parameters_and_agent_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_proxy(*_args: Any, **_kwargs: Any):
        raise AssertionError("invalid request must not be proxied")

    monkeypatch.setattr(responses_route, "proxy_to_assistant_service", should_not_proxy)
    with TestClient(_app(_user())) as client:
        query = client.post(
            "/v1/responses?foo=bar",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )
        forged = client.post(
            "/v1/responses",
            json={
                "model": "qwen3.7-plus",
                "input": "hello",
                "runtime_envelope": {"forged": True},
            },
        )

    assert query.status_code == 400
    assert query.json()["error"]["code"] == "unsupported_query_parameters"
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "agent_runtime_field_forbidden"


def test_gateway_application_registers_exact_v1_responses_path() -> None:
    from src.main import create_app

    paths = create_app().openapi()["paths"]
    assert "/v1/responses" in paths
    assert set(paths["/v1/responses"]) == {"post"}
    assert "/api/v1/responses" not in paths


def json_bytes(value: bytes) -> dict[str, Any]:
    import json

    return json.loads(value)
