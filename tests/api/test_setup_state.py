"""First-run onboarding state endpoint tests.

Follows the pattern of ``test_assistant_local_nodes_gateway.py``: a bare
``FastAPI()`` app with ``include_router`` plus dependency overrides.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_user_context
from src.api.v1 import setup as setup_api
from src.config.settings import AuthAPIKeySettings, AuthenticationSettings, Settings
from src.core.auth.user_resolver import UserContext

KNOWN_PROVIDERS = {
    "openai",
    "anthropic",
    "deepseek",
    "dashscope",
    "google",
    "google-vertex",
}


class _StubModelMeta:
    """Minimal stand-in for the GatewayModelMeta facade."""

    def __init__(self, *, enabled: list[str] | None = None, configured: list[str] | None = None):
        self._enabled = set(enabled or [])
        self._configured = set(configured or [])

    async def list_enabled_providers(self, _tenant_id: str) -> list[str]:
        return list(self._enabled)

    async def is_provider_configured(self, _tenant_id: str, provider_id: str) -> bool:
        return provider_id in self._configured


def _client(
    model_meta: _StubModelMeta | None = None,
    *,
    settings: Settings | None = None,
    use_api_key: str | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(setup_api.router)
    if model_meta is not None:
        app.state.model_meta = model_meta
    if use_api_key is not None:
        app.state.settings = settings or Settings(
            authentication=AuthenticationSettings(
                api_key=AuthAPIKeySettings(enabled=True, keys=[use_api_key])
            )
        )
    else:
        app.dependency_overrides[get_user_context] = lambda: UserContext(
            tenant_id="tenant-gateway",
            user_id="user-gateway",
            is_authenticated=True,
        )
        app.state.settings = settings or Settings()
    return TestClient(app)


def test_setup_state_reports_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    client = _client(_StubModelMeta())

    response = client.get("/setup/state")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert sorted(body["missing"]) == sorted(KNOWN_PROVIDERS)
    assert body["mode"] == "ui"
    # Effective deployment default (code default when the env var is unset).
    assert body["default_model"] == "qwen3.7-plus"


def test_setup_state_reports_configured_when_provider_has_credential() -> None:
    client = _client(
        _StubModelMeta(enabled=["dashscope", "openai"], configured=["dashscope"])
    )

    response = client.get("/setup/state")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert "dashscope" not in body["missing"]
    assert sorted(body["missing"]) == sorted(KNOWN_PROVIDERS - {"dashscope"})


def test_setup_state_is_graceful_without_model_meta() -> None:
    client = _client()  # app.state.model_meta never set

    response = client.get("/setup/state")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert sorted(body["missing"]) == sorted(KNOWN_PROVIDERS)


def test_setup_state_exposes_environment_mode() -> None:
    client = _client(
        _StubModelMeta(enabled=["dashscope"], configured=["dashscope"]),
        settings=Settings(model_setup_mode="environment"),
    )

    response = client.get("/setup/state")

    assert response.status_code == 200
    assert response.json()["mode"] == "environment"


def test_setup_state_returns_default_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_MODEL", "default-model-from-env")
    client = _client(_StubModelMeta())

    response = client.get("/setup/state")

    assert response.status_code == 200
    assert response.json()["default_model"] == "default-model-from-env"


def test_setup_state_accepts_api_key_auth() -> None:
    """API-key authentication reaches the endpoint without admin gating."""
    client = _client(_StubModelMeta(), use_api_key="test-key")

    response = client.get("/setup/state", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200
    assert response.json()["configured"] is False


def test_setup_state_rejects_anonymous_guests() -> None:
    app = FastAPI()
    app.include_router(setup_api.router)
    app.state.settings = Settings()
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        tenant_id="public",
        user_id="anon:guest",
        is_authenticated=False,
    )
    client = TestClient(app)

    response = client.get("/setup/state")

    assert response.status_code == 401
