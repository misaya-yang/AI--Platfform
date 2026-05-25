from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (
    REPO_ROOT / "packages" / "ai-gateway-core" / "src",
    REPO_ROOT / "apps" / "assistant-service" / "src",
):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from assistant_service.core.tools import (
    gemini_image_tool,
    image_generator_tool,
    smart_image_generator,
)
from assistant_service.core.tools.gemini_image_tool import GeminiImageResult
from assistant_service.core.tools.image_generator_tool import ImageGenerationResult
from assistant_service.core.tools.smart_image_generator import SmartImageGenerator


@pytest.mark.asyncio
async def test_google_image_override_uses_gateway_model_and_key(monkeypatch) -> None:
    instances: list[Any] = []

    class FakeGemini:
        def __init__(
            self,
            *,
            api_key: str,
            model: str | None = None,
            base_url: str | None = None,
            backend: str | None = None,
        ) -> None:
            self.api_key = api_key
            self.model = model
            self.base_url = base_url
            self.backend = backend
            instances.append(self)

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return GeminiImageResult(
                success=True,
                images=[{"content_base64": "abc", "mime_type": "image/png"}],
                duration_ms=12,
            )

    monkeypatch.setattr(gemini_image_tool, "GeminiImageGenerator", FakeGemini)

    result = await SmartImageGenerator().generate(
        prompt="cat",
        image_model_override={
            "enabled": True,
            "provider_id": "google",
            "provider": "gemini",
            "model_id": "gemini-3.1-flash-image-preview",
            "base_url": "https://generativelanguage.googleapis.com",
            "_api_key": "gateway-secret",
        },
    )

    assert result.success is True
    assert result.provider == "google"
    assert instances[0].api_key == "gateway-secret"
    assert instances[0].model == "gemini-3.1-flash-image-preview"
    assert instances[0].backend == "ai_studio"


@pytest.mark.asyncio
async def test_dashscope_image_override_uses_gateway_model_key_and_base(monkeypatch) -> None:
    instances: list[Any] = []

    class FakeDashScope:
        def __init__(
            self,
            *,
            api_key: str,
            model: str | None = None,
            base_url: str | None = None,
        ) -> None:
            self.api_key = api_key
            self.model = model
            self.base_url = base_url
            instances.append(self)

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return ImageGenerationResult(
                success=True,
                images=[{"content_base64": "abc", "mime_type": "image/png"}],
                duration_ms=20,
            )

    monkeypatch.setattr(image_generator_tool, "DashScopeImageGenerator", FakeDashScope)

    result = await SmartImageGenerator().generate(
        prompt="cat",
        image_model_override={
            "enabled": True,
            "provider_id": "dashscope",
            "provider": "dashscope",
            "model_id": "qwen-image-2.0",
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "_api_key": "gateway-secret",
        },
    )

    assert result.success is True
    assert result.provider == "dashscope"
    assert instances[0].api_key == "gateway-secret"
    assert instances[0].model == "qwen-image-2.0"
    assert instances[0].base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert instances[0].kwargs["model_override"] == "qwen-image-2.0"


@pytest.mark.asyncio
async def test_default_dashscope_routing_keeps_image_model_override(monkeypatch) -> None:
    class FakeProvider:
        is_configured = False

    class FakeDashScope:
        is_configured = True

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return ImageGenerationResult(
                success=True,
                images=[{"content_base64": "abc", "mime_type": "image/png"}],
                duration_ms=18,
            )

    dash = FakeDashScope()
    monkeypatch.setattr(smart_image_generator, "get_gemini_image_generator", lambda: FakeProvider())
    monkeypatch.setattr(smart_image_generator, "get_doubao_image_generator", lambda: FakeProvider())
    monkeypatch.setattr(smart_image_generator, "get_image_generator", lambda: dash)

    result = await SmartImageGenerator().generate(
        prompt="cat",
        prefer_gemini=False,
        dashscope_model="qwen-image-2.0",
    )

    assert result.success is True
    assert result.provider == "dashscope"
    assert dash.kwargs["model_override"] == "qwen-image-2.0"
