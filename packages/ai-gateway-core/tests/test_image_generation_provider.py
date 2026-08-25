from __future__ import annotations

import base64
import json

import httpx
import pytest
from ai_gateway_core.media.image_generation import (
    ImageGenerationConfig,
    ImageGenerationProvider,
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_google_inline_image_uses_configured_provider_without_fallback():
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "secret-is-not-in-result"
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]},
        )

    client = _client(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig(
            "google",
            "secret-is-not-in-result",
            "https://generativelanguage.googleapis.com",
            "image-model",
        ),
        client=client,
    )
    result = await provider.generate(prompt="a red kite")
    await client.aclose()
    assert result.success is True
    assert result.provider == "google"
    assert result.error is None


@pytest.mark.asyncio
async def test_google_reference_uses_inline_data_part():
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}
        )

    client = _client(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig(
            "google", "key", "https://google.example", "image-model", supports_reference_images=True
        ),
        client=client,
    )
    result = await provider.generate(
        prompt="edit", reference_image=b"\x89PNG\r\n\x1a\n", reference_mime="image/png"
    )
    await client.aclose()
    assert result.success is True
    assert seen["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/png"


@pytest.mark.asyncio
async def test_reference_unsupported_fails_before_http_call():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("reference must be rejected locally")

    client = _client(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig("doubao", "key", "https://ark.example", "image-model"), client=client
    )
    result = await provider.generate(
        prompt="edit", reference_image=b"\x89PNG\r\n\x1a\n", reference_mime="image/png"
    )
    await client.aclose()
    assert calls == 0
    assert result.error_code == "reference_unsupported"


@pytest.mark.asyncio
async def test_reference_mime_and_size_rejected_before_http_call():
    client = _client(lambda _request: httpx.Response(500))
    provider = ImageGenerationProvider(
        ImageGenerationConfig(
            "google", "key", "https://google.example", "image-model", supports_reference_images=True
        ),
        client=client,
    )
    result = await provider.generate(
        prompt="edit", reference_image=b"bad", reference_mime="image/png"
    )
    await client.aclose()
    assert result.error_code == "invalid_reference"


@pytest.mark.asyncio
async def test_url_only_response_is_rejected_without_download():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image.png"}]})

    client = _client(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig("doubao", "key", "https://ark.example", "image-model"), client=client
    )
    result = await provider.generate(prompt="a red kite")
    await client.aclose()
    assert calls == 1
    assert result.error_code == "url_fetch_failed"
    assert result.images == []


@pytest.mark.asyncio
async def test_no_cross_provider_fallback_on_http_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "secret"}})

    client = _client(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig("doubao", "key", "https://ark.example", "image-model"), client=client
    )
    result = await provider.generate(prompt="a red kite")
    await client.aclose()
    assert result.provider == "doubao"
    assert result.error_code == "provider_http_error"
    assert result.outcome_unknown is True
    assert "secret" not in (result.error or "")


def test_environment_defaults_to_configured_dashscope_provider(monkeypatch):
    for name in (
        "IMAGE_GENERATION_PROVIDER",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VERTEX_API_KEY",
        "VERTEX_IMAGE_API_KEY",
        "ARK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DASHSCOPE_IMAGE_API_KEY", "dashscope-image-key")
    monkeypatch.setenv("DASHSCOPE_IMAGE_MODEL", "wan2.6-t2i")

    config = ImageGenerationConfig.from_environment()

    assert config.provider == "dashscope"
    assert config.api_key == "dashscope-image-key"
    assert config.dashscope_protocol == "wan26"


@pytest.mark.asyncio
async def test_invalid_endpoint_is_blocked_before_http_call():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    client = _client(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig("google", "key", "http://127.0.0.1:8080", "image-model"),
        client=client,
    )
    result = await provider.generate(prompt="a red kite")
    await client.aclose()
    assert result.error_code == "provider_endpoint_invalid"


@pytest.mark.asyncio
async def test_owned_client_is_reused_and_closed():
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}
        )

    transport = httpx.MockTransport(handler)
    provider = ImageGenerationProvider(
        ImageGenerationConfig("google", "key", "https://google.example", "image-model")
    )
    provider._client = httpx.AsyncClient(transport=transport)
    result = await provider.generate(prompt="a")
    await provider.close()
    assert result.success is True
    assert requests == 1
