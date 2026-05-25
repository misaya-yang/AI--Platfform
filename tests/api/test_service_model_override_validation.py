from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from src.api.deps import AuthContext
from src.api.v1.services import register_service, update_service
from src.config.settings import Settings
from src.core.auth.rbac import RBAC
from src.services.registry.service_registry import MemoryRegistryStorage, ServiceRegistry


class FakeProviderService:
    def __init__(self, providers: dict[str, dict[str, Any]] | None = None):
        self.providers = providers or {}

    async def get_provider(self, tenant_id: str, provider_id: str) -> dict[str, Any] | None:
        assert tenant_id == "tenant-a"
        return self.providers.get(provider_id)

    def allows_environment_credentials(self, provider: dict[str, Any]) -> bool:
        return bool(
            provider.get("allow_environment_credentials")
            or provider.get("uses_environment_credentials")
        )


class FakeModelService:
    def __init__(self, models: dict[tuple[str, str], dict[str, Any]] | None = None):
        self.models = models or {}

    async def get_provider_model(
        self,
        tenant_id: str,
        provider_id: str,
        model_id: str,
    ) -> dict[str, Any] | None:
        assert tenant_id == "tenant-a"
        return self.models.get((provider_id, model_id))


def _auth() -> AuthContext:
    return AuthContext(
        user_id="admin-1",
        tenant_id="tenant-a",
        roles=["admin"],
        permissions=[],
    )


def _request(
    *,
    provider_service: FakeProviderService | None = None,
    model_service: FakeModelService | None = None,
) -> SimpleNamespace:
    settings = Settings()
    request = SimpleNamespace()
    request.app = SimpleNamespace()
    request.app.state = SimpleNamespace()
    request.app.state.dispatcher = SimpleNamespace(
        rbac=RBAC(role_permissions=settings.rbac.roles)
    )
    if provider_service is not None:
        request.app.state.provider_service = provider_service
    if model_service is not None:
        request.app.state.model_service = model_service
    request.state = SimpleNamespace(request_id="req-model-override")
    return request


def _provider(
    provider_id: str = "dashscope-prod",
    *,
    enabled: bool = True,
    has_api_key: bool = True,
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "api_type": "dashscope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "is_enabled": enabled,
        "has_api_key": has_api_key,
    }


def _model(
    provider_id: str = "dashscope-prod",
    model_id: str = "qwen-max",
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "is_enabled": enabled,
    }


def _definition(model_override: dict[str, Any]) -> dict[str, Any]:
    return {
        "service_id": "imam-agent",
        "name": "Imam Agent",
        "service_type": "langgraph",
        "supported_modes": ["sync", "stream"],
        "connector_type": "http",
        "connector_config": {
            "base_url": "http://imam-agent:8000",
            "graph_id": "Imam",
            "model_override": model_override,
        },
        "accepted_content_types": ["text"],
        "output_content_types": ["text"],
        "metadata": {"adapter_type": "langgraph"},
    }


@pytest.mark.asyncio
async def test_register_service_rejects_browser_supplied_api_key():
    registry = ServiceRegistry(MemoryRegistryStorage())
    request = _request(
        provider_service=FakeProviderService(),
        model_service=FakeModelService(),
    )

    with pytest.raises(HTTPException) as exc:
        await register_service(
            request=request,
            definition=_definition(
                {
                    "enabled": True,
                    "provider_id": "dashscope-prod",
                    "model_id": "qwen-max",
                    "_api_key": "browser-secret",
                }
            ),
            registry=registry,
            auth=_auth(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "MODEL_OVERRIDE_API_KEY_FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("providers", "models", "expected_detail"),
    [
        ({}, {}, "MODEL_OVERRIDE_PROVIDER_NOT_FOUND"),
        (
            {"dashscope-prod": _provider(enabled=False)},
            {},
            "MODEL_OVERRIDE_PROVIDER_DISABLED",
        ),
        (
            {"dashscope-prod": _provider(has_api_key=False)},
            {},
            "MODEL_OVERRIDE_API_KEY_MISSING",
        ),
        (
            {"dashscope-prod": _provider()},
            {("other-provider", "qwen-max"): _model("other-provider", "qwen-max")},
            "MODEL_OVERRIDE_MODEL_NOT_FOUND",
        ),
        (
            {"dashscope-prod": _provider()},
            {("dashscope-prod", "qwen-max"): _model(enabled=False)},
            "MODEL_OVERRIDE_MODEL_DISABLED",
        ),
    ],
)
async def test_register_service_validates_provider_model_and_key(
    providers: dict[str, dict[str, Any]],
    models: dict[tuple[str, str], dict[str, Any]],
    expected_detail: str,
):
    registry = ServiceRegistry(MemoryRegistryStorage())
    request = _request(
        provider_service=FakeProviderService(providers),
        model_service=FakeModelService(models),
    )

    with pytest.raises(HTTPException) as exc:
        await register_service(
            request=request,
            definition=_definition(
                {
                    "enabled": True,
                    "provider_id": "dashscope-prod",
                    "model_id": "qwen-max",
                }
            ),
            registry=registry,
            auth=_auth(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == expected_detail


@pytest.mark.asyncio
async def test_register_service_stores_valid_override_without_secret():
    registry = ServiceRegistry(MemoryRegistryStorage())
    request = _request(
        provider_service=FakeProviderService({"dashscope-prod": _provider()}),
        model_service=FakeModelService({("dashscope-prod", "qwen-max"): _model()}),
    )

    result = await register_service(
        request=request,
        definition=_definition(
            {
                "enabled": "true",
                "provider_id": "dashscope-prod",
                "model_id": "qwen-max",
                "temperature": "0.3",
            }
        ),
        registry=registry,
        auth=_auth(),
    )
    stored = await registry.get("imam-agent")
    model_override = stored.connector_config["model_override"]

    assert result == {"service_id": "imam-agent", "status": "registered"}
    assert model_override["enabled"] is True
    assert model_override["temperature"] == 0.3
    assert model_override["cache_epoch"] == 1
    assert "_api_key" not in model_override
    assert "api_key" not in model_override


@pytest.mark.asyncio
async def test_disabled_override_can_be_saved_without_provider_lookup():
    registry = ServiceRegistry(MemoryRegistryStorage())
    request = _request()

    await register_service(
        request=request,
        definition=_definition(
            {
                "enabled": False,
                "provider_id": "missing-provider",
                "model_id": "missing-model",
            }
        ),
        registry=registry,
        auth=_auth(),
    )
    stored = await registry.get("imam-agent")
    model_override = stored.connector_config["model_override"]

    assert model_override["enabled"] is False
    assert model_override["cache_epoch"] == 1


@pytest.mark.asyncio
async def test_update_service_increments_cache_epoch_when_override_changes():
    registry = ServiceRegistry(MemoryRegistryStorage())
    initial = registry._service_from_dict(
        _definition(
            {
                "enabled": True,
                "provider_id": "dashscope-prod",
                "model_id": "qwen-old",
                "cache_epoch": 4,
            }
        )
    )
    await registry.register(initial)
    request = _request(
        provider_service=FakeProviderService({"dashscope-prod": _provider()}),
        model_service=FakeModelService(
            {
                ("dashscope-prod", "qwen-new"): _model(model_id="qwen-new"),
            }
        ),
    )

    result = await update_service(
        service_id="imam-agent",
        request=request,
        patch={
            "connector_config": {
                "base_url": "http://imam-agent:8000",
                "graph_id": "Imam",
                "model_override": {
                    "enabled": True,
                    "provider_id": "dashscope-prod",
                    "model_id": "qwen-new",
                    "cache_epoch": 999,
                },
            }
        },
        registry=registry,
        auth=_auth(),
    )
    stored = await registry.get("imam-agent")
    model_override = stored.connector_config["model_override"]

    assert result == {"status": "success", "service_id": "imam-agent"}
    assert model_override["model_id"] == "qwen-new"
    assert model_override["cache_epoch"] == 5
