from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from src.api.schemas.assistant import ImageGenerationRequest
from src.api.v1.assistant import (
    _build_image_generation_proxy_body,
    _validate_image_model_override,
)
from src.core.auth.user_resolver import UserContext


class FakeProviderService:
    def __init__(self, *, api_key: str | None = "gateway-image-secret") -> None:
        self.api_key = api_key

    async def get_runtime_provider_config(self, tenant_id: str, provider_id: str) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        if provider_id == "missing":
            raise ValueError("missing provider")
        return {
            "is_enabled": True,
            "runtime_provider": "gemini" if provider_id == "google" else provider_id,
            "runtime_base_url": "https://generativelanguage.googleapis.com",
            "api_key": self.api_key,
            "allow_environment_credentials": False,
        }


class FakeModelService:
    def __init__(self, *, model_type: str = "image") -> None:
        self.model_type = model_type

    async def get_provider_model(
        self,
        tenant_id: str,
        provider_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert provider_id in {"google", "dashscope"}
        return {
            "model_id": model_id,
            "provider_id": provider_id,
            "is_enabled": True,
            "model_type": self.model_type,
        }


class FakeRequest:
    def __init__(self, payload: dict[str, Any], state: SimpleNamespace) -> None:
        self._payload = payload
        self.app = SimpleNamespace(state=state)

    async def json(self) -> dict[str, Any]:
        return self._payload


def _state(*, model_type: str = "image", api_key: str | None = "gateway-image-secret"):
    return SimpleNamespace(
        provider_service=FakeProviderService(api_key=api_key),
        model_service=FakeModelService(model_type=model_type),
        assistant_image_model_override={
            "enabled": True,
            "provider_id": "google",
            "model_id": "gemini-3.1-flash-image-preview",
        },
    )


def _user() -> UserContext:
    return UserContext(user_id="u1", tenant_id="tenant-a", roles=["admin"], tier="admin")


@pytest.mark.asyncio
async def test_image_generation_uses_stored_gateway_image_override() -> None:
    request = FakeRequest(
        {"prompt": "cat", "model_id": "qwen-image-2.0", "n": 1},
        _state(),
    )
    body = ImageGenerationRequest(prompt="cat", model_id="qwen-image-2.0", n=1)

    encoded = await _build_image_generation_proxy_body(request, _user(), body)
    payload = json.loads(encoded.decode("utf-8"))

    hejaz_image_model = payload["hejaz_image_model"]
    assert payload["model_id"] == "gemini-3.1-flash-image-preview"
    assert "image_model_override" not in payload
    assert hejaz_image_model["tenant_id"] == "tenant-a"
    assert hejaz_image_model["provider_id"] == "google"
    assert hejaz_image_model["provider"] == "gemini"
    assert hejaz_image_model["model_id"] == "gemini-3.1-flash-image-preview"
    assert hejaz_image_model["_api_key"] == "gateway-image-secret"
    assert (
        hejaz_image_model["api_key_fingerprint"]
        == hashlib.sha256(b"gateway-image-secret").hexdigest()[:16]
    )


@pytest.mark.asyncio
async def test_browser_secret_fields_are_rejected_in_public_image_override() -> None:
    request = FakeRequest(
        {
            "prompt": "cat",
            "model_id": "qwen-image-2.0",
            "image_model_override": {
                "enabled": True,
                "provider_id": "google",
                "model_id": "gemini-3.1-flash-image-preview",
                "_api_key": "browser-secret",
            },
        },
        _state(),
    )
    body = ImageGenerationRequest(prompt="cat", model_id="qwen-image-2.0", n=1)

    with pytest.raises(HTTPException) as exc:
        await _build_image_generation_proxy_body(request, _user(), body)

    assert exc.value.status_code == 422
    assert exc.value.detail == "IMAGE_MODEL_API_KEY_FORBIDDEN"


@pytest.mark.asyncio
async def test_browser_internal_image_config_is_scrubbed_when_override_disabled() -> None:
    request = FakeRequest(
        {
            "prompt": "cat",
            "model_id": "qwen-image-2.0",
            "image_model_override": {"enabled": False},
            "hejaz_image_model": {"_api_key": "browser-secret"},
        },
        _state(),
    )
    body = ImageGenerationRequest(prompt="cat", model_id="qwen-image-2.0", n=1)

    encoded = await _build_image_generation_proxy_body(request, _user(), body)
    payload = json.loads(encoded.decode("utf-8"))

    assert "hejaz_image_model" not in payload
    assert "image_model_override" not in payload
    assert "browser-secret" not in encoded.decode("utf-8")


@pytest.mark.asyncio
async def test_non_image_model_is_rejected_before_assistant_service() -> None:
    request = FakeRequest({}, _state(model_type="llm"))

    with pytest.raises(HTTPException) as exc:
        await _validate_image_model_override(
            request,
            tenant_id="tenant-a",
            raw_override={
                "enabled": True,
                "provider_id": "google",
                "model_id": "gemini-3.1-pro-preview",
            },
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "IMAGE_MODEL_NOT_IMAGE_CAPABLE"


@pytest.mark.asyncio
async def test_missing_image_provider_key_is_rejected() -> None:
    request = FakeRequest({}, _state(api_key=None))

    with pytest.raises(HTTPException) as exc:
        await _validate_image_model_override(
            request,
            tenant_id="tenant-a",
            raw_override={
                "enabled": True,
                "provider_id": "google",
                "model_id": "gemini-3.1-flash-image-preview",
            },
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "IMAGE_MODEL_API_KEY_MISSING"
