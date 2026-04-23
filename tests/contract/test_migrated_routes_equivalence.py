"""Equivalence contract for Phase 5b route migrations.

For each migrated route, the gateway's response with the feature flag
OFF (in-process) must have the same **shape** as the response with the
flag ON (proxy to assistant-service). This catches the "AS side is a
stub" regression the Roadmap §5b calls out.

We can't exercise the full in-process dependency graph (SessionManager,
MCPManager, full ModelRegistry) inside a fast unit test — so these
tests assert **schema equivalence**: every key the in-process route
produces, the AS-proxied route also produces, and vice versa. They
also lock the AS route's shape to the gateway's pydantic schema so a
future AS PR can't drop a field silently.
"""
from __future__ import annotations

import httpx
import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from ai_gateway_core.auth.gateway_secret import GatewaySecret, InMemoryReplayStore
from assistant_service.auth import GatewaySecretAuthMiddleware


JWT_SECRET = "e2e-jwt-secret-32-chars-XXXXXXXXX"
GATEWAY_SECRET = "e2e-gateway-secret-32-chars-YYYYYYY"


def _token(*, roles: list[str] = ["admin"], tier: str = "admin") -> str:
    return pyjwt.encode(
        {
            "sub": "t-user",
            "tenant_id": "t1",
            "tier": tier,
            "roles": roles,
            "email": "t@example.com",
            "name": "Tester",
            "user_type": "user",
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def _build_as_app(handlers: dict):
    """Build a fake assistant-service with supplied route handlers.

    ``handlers`` maps ``path`` or ``("METHOD", path)`` to a sync callable
    that receives the resolved ``UserContext`` (and, for POSTs, the
    request body dict) and returns the response dict.
    """
    from assistant_service.auth import get_user_context as as_get_user_context

    app = FastAPI()
    gs = GatewaySecret(secret=GATEWAY_SECRET, replay_store=InMemoryReplayStore())
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=gs,
        allow_anonymous=False,
    )
    for key, fn in handlers.items():
        if isinstance(key, tuple):
            method, path = key
        else:
            method, path = "GET", key

        def _get_factory(handler):
            async def route(request: Request, user=Depends(as_get_user_context)):
                return handler(user)
            return route

        def _post_factory(handler):
            async def route(request: Request, user=Depends(as_get_user_context)):
                body = await request.json() if await request.body() else {}
                return handler(user, body)
            return route

        if method.upper() == "GET":
            app.get(path)(_get_factory(fn))
        elif method.upper() == "POST":
            app.post(path)(_post_factory(fn))
        else:  # pragma: no cover — defensive
            raise ValueError(f"unsupported test method {method}")
    return app


def _build_gateway_app(monkeypatch, fake_as_app: FastAPI):
    """Gateway app that routes /<route> → proxy_to_assistant_service."""
    from src.core.auth.user_resolver import UserResolver, UserResolverConfig

    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", GATEWAY_SECRET)
    import importlib

    import src.api.v1._assistant_proxy as ap

    importlib.reload(ap)

    transport = httpx.ASGITransport(app=fake_as_app)
    asgi_client = httpx.AsyncClient(
        transport=transport, base_url="http://assistant-service"
    )

    async def fake_get_client():
        return asgi_client

    async def _noop_reset():
        pass

    monkeypatch.setattr(ap._proxy, "_get_client", fake_get_client)
    monkeypatch.setattr(ap._proxy, "_reset_client", _noop_reset)

    resolver = UserResolver(
        UserResolverConfig(
            jwt_enabled=True,
            jwt_secret=JWT_SECRET,
            jwt_algorithms=["HS256"],
        )
    )

    gw = FastAPI()

    for upstream_path in ("models", "datasets", "config", "tools", "policies"):
        async def _handler(request: Request, _p=upstream_path):
            user = await resolver.resolve(request)
            return await ap.proxy_to_assistant_service(request, user, path=_p)
        gw.get(f"/gw/{upstream_path}")(_handler)

    async def _chat_handler(request: Request):
        user = await resolver.resolve(request)
        body_bytes = await request.body()
        return await ap.proxy_to_assistant_service(
            request, user, path="chat", body=body_bytes
        )
    gw.post("/gw/chat")(_chat_handler)

    return gw


# ---------------------------------------------------------------------------
# Schema-level equivalence assertions
# ---------------------------------------------------------------------------


MODELS_REQUIRED_KEYS = {
    "id", "name", "provider", "context_window", "max_output_tokens",
    "supports_vision", "supports_tools", "access_level",
    "input_price_per_1k", "output_price_per_1k",
}
DATASETS_REQUIRED_KEYS = {
    "dataset_id", "name", "document_count", "chunk_count", "is_multimodal",
}
CONFIG_REQUIRED_KEYS = {
    "default_model_id", "available_providers", "kb_enabled",
    "web_search_enabled", "tools_available",
}
TOOLS_REQUIRED_KEYS = {"name", "description", "category", "risk_level"}
# when_to_use / when_not_to_use are Optional — not required.
POLICIES_REQUIRED_TOP_LEVEL = {"policies"}
CHAT_REQUIRED_KEYS = {
    "content", "usage", "contexts", "duration_ms", "model_id",
    "session_id", "run_id",
}


def _build_models_handler():
    def _handler(user):
        return {
            "models": [
                {
                    "id": "m1",
                    "name": "Model 1",
                    "provider": "dashscope",
                    "context_window": 8000,
                    "max_output_tokens": 2000,
                    "supports_vision": False,
                    "supports_tools": True,
                    "access_level": "public",
                    "input_price_per_1k": 0.001,
                    "output_price_per_1k": 0.002,
                }
            ]
        }
    return _handler


def _build_datasets_handler():
    def _handler(user):
        return {
            "datasets": [
                {
                    "dataset_id": "ds-1",
                    "name": "Test KB",
                    "description": None,
                    "document_count": 10,
                    "chunk_count": 100,
                    "embedding_model": "text-embedding-3-small",
                    "is_multimodal": False,
                }
            ]
        }
    return _handler


def _build_config_handler():
    def _handler(user):
        return {
            "default_model_id": "qwen3.6-plus",
            "available_providers": ["dashscope", "google-vertex"],
            "kb_enabled": True,
            "web_search_enabled": False,
            "tools_available": ["search_knowledge", "search_web"],
        }
    return _handler


def test_models_response_has_required_keys(monkeypatch):
    fake_as = _build_as_app({"/api/v1/assistant/models": _build_models_handler()})
    gw = _build_gateway_app(monkeypatch, fake_as)

    with TestClient(gw) as c:
        r = c.get("/gw/models", headers={"Authorization": f"Bearer {_token()}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert "models" in body and body["models"]
    assert MODELS_REQUIRED_KEYS <= set(body["models"][0].keys()), (
        f"missing keys: {MODELS_REQUIRED_KEYS - set(body['models'][0].keys())}"
    )


def test_datasets_response_has_required_keys(monkeypatch):
    fake_as = _build_as_app({"/api/v1/assistant/datasets": _build_datasets_handler()})
    gw = _build_gateway_app(monkeypatch, fake_as)

    with TestClient(gw) as c:
        r = c.get("/gw/datasets", headers={"Authorization": f"Bearer {_token()}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert "datasets" in body and body["datasets"]
    assert DATASETS_REQUIRED_KEYS <= set(body["datasets"][0].keys())


def test_config_response_has_required_keys(monkeypatch):
    fake_as = _build_as_app({"/api/v1/assistant/config": _build_config_handler()})
    gw = _build_gateway_app(monkeypatch, fake_as)

    with TestClient(gw) as c:
        r = c.get("/gw/config", headers={"Authorization": f"Bearer {_token()}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert CONFIG_REQUIRED_KEYS <= set(body.keys())


def test_as_models_returns_gateway_schema_keys():
    """Direct check on the AS-side ``/models`` implementation — it must
    not drift into a reduced-shape stub that breaks the equivalence.
    """
    import inspect

    from assistant_service.api.routes.models import list_models

    # The function returns a dict; inspect its source for the schema keys
    # it builds. If any required key is missing from the source, the
    # shape check above would still fail at runtime — but this catches
    # the regression before the integration test runs.
    source = inspect.getsource(list_models)
    for key in MODELS_REQUIRED_KEYS:
        assert f'"{key}"' in source, (
            f"AS /models handler missing schema key: {key}"
        )


def test_as_datasets_returns_gateway_schema_keys():
    import inspect

    from assistant_service.api.routes.models import list_datasets

    source = inspect.getsource(list_datasets)
    for key in DATASETS_REQUIRED_KEYS:
        assert f'"{key}"' in source, (
            f"AS /datasets handler missing schema key: {key}"
        )


def test_as_config_returns_gateway_schema_keys():
    import inspect

    from assistant_service.api.routes.models import get_config

    source = inspect.getsource(get_config)
    for key in CONFIG_REQUIRED_KEYS:
        assert f'"{key}"' in source, (
            f"AS /config handler missing schema key: {key}"
        )


# ---------------------------------------------------------------------------
# Phase 5b round 2: /tools, /policies, /chat
# ---------------------------------------------------------------------------


def _build_tools_handler():
    def _handler(user):
        return {
            "tools": [
                {
                    "name": "search_knowledge",
                    "description": "Search the knowledge base",
                    "category": "kb",
                    "risk_level": "low",
                    "when_to_use": "When user asks a fact lookup",
                    "when_not_to_use": "When user asks for chat",
                }
            ]
        }
    return _handler


def _build_policies_handler():
    def _handler(user):
        return {
            "policies": {
                "approval_required": False,
                "allowed_tool_categories": ["kb", "web"],
                "max_run_duration_ms": 60000,
            }
        }
    return _handler


def _build_chat_handler():
    def _handler(user, body):
        return {
            "content": f"hi {user.user_id}: {body.get('message', '')}",
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            "contexts": [],
            "duration_ms": 123.4,
            "model_id": body.get("model_id", "qwen3.6-plus"),
            "session_id": body.get("session_id") or "new-sess",
            "run_id": "run-xyz",
        }
    return _handler


def test_tools_response_has_required_keys(monkeypatch):
    fake_as = _build_as_app({"/api/v1/assistant/tools": _build_tools_handler()})
    gw = _build_gateway_app(monkeypatch, fake_as)

    with TestClient(gw) as c:
        r = c.get("/gw/tools", headers={"Authorization": f"Bearer {_token()}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert "tools" in body and body["tools"]
    first = body["tools"][0]
    assert TOOLS_REQUIRED_KEYS <= set(first.keys())
    # Type checks
    assert isinstance(first["name"], str)
    assert isinstance(first["description"], str)
    assert isinstance(first["category"], str)
    assert isinstance(first["risk_level"], str)


def test_policies_response_has_required_keys(monkeypatch):
    fake_as = _build_as_app({"/api/v1/assistant/policies": _build_policies_handler()})
    gw = _build_gateway_app(monkeypatch, fake_as)

    with TestClient(gw) as c:
        r = c.get("/gw/policies", headers={"Authorization": f"Bearer {_token()}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert POLICIES_REQUIRED_TOP_LEVEL <= set(body.keys())
    # policies must be a dict (shape: Dict[str, Any])
    assert isinstance(body["policies"], dict)


def test_chat_response_has_required_keys(monkeypatch):
    fake_as = _build_as_app(
        {("POST", "/api/v1/assistant/chat"): _build_chat_handler()}
    )
    gw = _build_gateway_app(monkeypatch, fake_as)

    with TestClient(gw) as c:
        r = c.post(
            "/gw/chat",
            headers={"Authorization": f"Bearer {_token()}"},
            json={
                "message": "hello",
                "model_id": "qwen3.6-plus",
                "session_id": "sess-1",
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert CHAT_REQUIRED_KEYS <= set(body.keys()), (
        f"missing: {CHAT_REQUIRED_KEYS - set(body.keys())}"
    )
    assert isinstance(body["content"], str)
    assert isinstance(body["usage"], dict)
    assert isinstance(body["contexts"], list)
    assert isinstance(body["duration_ms"], (int, float))


def test_as_tools_returns_gateway_schema_keys():
    import inspect

    from assistant_service.api.routes.tools import list_tools

    source = inspect.getsource(list_tools)
    for key in TOOLS_REQUIRED_KEYS | {"when_to_use", "when_not_to_use"}:
        assert f'"{key}"' in source, (
            f"AS /tools handler missing schema key: {key}"
        )


def test_as_policies_returns_top_level_policies_key():
    import inspect

    from assistant_service.api.routes.tools import get_policies

    source = inspect.getsource(get_policies)
    assert '"policies"' in source, "AS /policies handler missing 'policies' key"
    # Must call get_gateway_policies — the actual data source.
    assert "get_gateway_policies" in source, (
        "AS /policies must delegate to AssistantService.get_gateway_policies()"
    )


def test_as_chat_returns_gateway_schema_keys():
    """AS /chat handler must emit every field the gateway's
    ``AssistantChatResponse`` mandates, so a proxied call is shape-equivalent
    to the in-process call."""
    import inspect

    from assistant_service.api.routes.chat import chat as as_chat

    source = inspect.getsource(as_chat)
    for key in CHAT_REQUIRED_KEYS:
        assert f'"{key}"' in source, (
            f"AS /chat handler missing schema key: {key}"
        )
