from __future__ import annotations

from types import SimpleNamespace

import pytest
from ai_gateway_core.enums import ModelAccessLevel, ModelProvider
from assistant_service.api.routes.models import get_config
from assistant_service.auth import UserContext
from assistant_service.core.models.model_registry import ModelInfo


class _FakeModelRegistry:
    def __init__(self, models: list[ModelInfo], configured: set[ModelProvider]):
        self._models = models
        self._configured = configured

    def is_provider_configured(self, provider: ModelProvider) -> bool:
        return provider in self._configured

    def get_available_models(self) -> list[ModelInfo]:
        return [
            model for model in self._models
            if model.provider in self._configured
        ]


def _request(model_registry: _FakeModelRegistry):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                model_registry=model_registry,
                kb_proxy=object(),
            )
        )
    )


def _user(**kwargs) -> UserContext:
    return UserContext(user_id="u1", tenant_id="default", **kwargs)


@pytest.mark.asyncio
async def test_config_uses_first_visible_available_model_as_default() -> None:
    model = ModelInfo(
        id="gpt-4o-mini",
        name="GPT-4o mini",
        provider=ModelProvider.OPENAI,
        access_level=ModelAccessLevel.PUBLIC,
    )
    registry = _FakeModelRegistry([model], {ModelProvider.OPENAI})

    body = await get_config(_request(registry), _user())

    assert body["default_model_id"] == "gpt-4o-mini"
    assert "openai" in body["available_providers"]


@pytest.mark.asyncio
async def test_config_prefers_qwen37_plus_when_dashscope_is_configured() -> None:
    gemini = ModelInfo(
        id="gemini-3-pro-preview",
        name="Gemini 3 Pro",
        provider=ModelProvider.GOOGLE,
        access_level=ModelAccessLevel.PUBLIC,
    )
    qwen = ModelInfo(
        id="qwen3.7-plus",
        name="Qwen 3.7 Plus",
        provider=ModelProvider.DASHSCOPE,
        access_level=ModelAccessLevel.PUBLIC,
    )
    registry = _FakeModelRegistry([gemini, qwen], {ModelProvider.GOOGLE, ModelProvider.DASHSCOPE})

    body = await get_config(_request(registry), _user())

    assert body["default_model_id"] == "qwen3.7-plus"


@pytest.mark.asyncio
async def test_config_returns_empty_default_when_no_models_match_configured_providers() -> None:
    model = ModelInfo(
        id="gemini-3-flash-preview",
        name="Gemini 3 Flash",
        provider=ModelProvider.GOOGLE,
        access_level=ModelAccessLevel.PUBLIC,
    )
    registry = _FakeModelRegistry([model], {ModelProvider.OPENAI})

    body = await get_config(_request(registry), _user())

    assert body["default_model_id"] == ""
    assert body["available_providers"] == ["openai"]
