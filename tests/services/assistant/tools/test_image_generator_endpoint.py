from __future__ import annotations

import json

import httpx
import pytest
from assistant_service.config.startup_fingerprint import resolve_startup_config
from assistant_service.core.tools.image_generator_tool import DashScopeImageGenerator


def test_default_image_model_is_current_international_wan(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_IMAGE_MODEL", raising=False)

    generator = DashScopeImageGenerator(api_key="test-explicit-key")

    assert generator.model == "wan2.6-t2i"
    assert resolve_startup_config({}).runtime_value("DASHSCOPE_IMAGE_MODEL") == "wan2.6-t2i"


@pytest.mark.parametrize(
    "configured_url",
    [
        "https://dashscope-intl.aliyuncs.com",
        "https://dashscope-intl.aliyuncs.com/api/v1",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    ],
)
def test_raw_image_client_builds_native_final_urls(monkeypatch, configured_url: str) -> None:
    monkeypatch.setenv("DASHSCOPE_IMAGE_API_KEY", "test-image-key")
    monkeypatch.setenv("DASHSCOPE_IMAGE_BASE_URL", configured_url)

    generator = DashScopeImageGenerator()

    assert generator.SUBMIT_URL == (
        "https://dashscope-intl.aliyuncs.com/api/v1/"
        "services/aigc/image-generation/generation"
    )
    assert generator.TASK_URL.format(task_id="task-123") == (
        "https://dashscope-intl.aliyuncs.com/api/v1/tasks/task-123"
    )


def test_explicit_image_key_uses_native_default_base() -> None:
    generator = DashScopeImageGenerator(api_key="test-explicit-key")

    assert generator.SUBMIT_URL == (
        "https://dashscope.aliyuncs.com/api/v1/"
        "services/aigc/image-generation/generation"
    )
    assert generator.TASK_URL.format(task_id="task-123") == (
        "https://dashscope.aliyuncs.com/api/v1/tasks/task-123"
    )


def _mock_image_client(
    poll_output: dict[str, object],
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task-123"}})
        if request.url.path.endswith("/tasks/task-123"):
            return httpx.Response(
                200,
                json={"output": {"task_status": "SUCCEEDED", **poll_output}},
            )
        return httpx.Response(
            200,
            content=b"generated-image-bytes",
            headers={"content-type": "image/png"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


@pytest.mark.asyncio
async def test_wan26_uses_current_async_protocol_and_choices_result() -> None:
    image_url = "https://cdn.example.test/wan26.png"
    client, requests = _mock_image_client(
        {
            "choices": [
                {
                    "message": {
                        "content": [{"image": image_url}],
                    }
                }
            ]
        }
    )
    generator = DashScopeImageGenerator(
        api_key="test-explicit-key",
        model="wan2.6-t2i",
        base_url="https://dashscope-intl.aliyuncs.com/api/v1",
    )
    generator._client = client

    try:
        result = await generator.generate(
            prompt="A blue cup",
            negative_prompt="text, logo",
            size="1536*1536",
            style="<photography>",
            n=2,
        )
    finally:
        await generator.close()

    submit = next(request for request in requests if request.method == "POST")
    payload = json.loads(submit.content)

    assert result.success is True
    assert len(result.images) == 1
    assert submit.url.path == "/api/v1/services/aigc/image-generation/generation"
    assert payload == {
        "model": "wan2.6-t2i",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": "A blue cup"}],
                }
            ]
        },
        "parameters": {
            "negative_prompt": "text, logo",
            "size": "1280*1280",
            "n": 2,
            "prompt_extend": True,
            "watermark": False,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["1024*1024", "1280*1280", "1280*720", "720*1280"])
async def test_wan26_preserves_explicit_valid_size(size: str) -> None:
    image_url = "https://cdn.example.test/wan26.png"
    client, requests = _mock_image_client({"results": [{"url": image_url}]})
    generator = DashScopeImageGenerator(
        api_key="test-explicit-key",
        model="wan2.6-t2i",
        base_url="https://dashscope-intl.aliyuncs.com/api/v1",
    )
    generator._client = client

    try:
        result = await generator.generate(prompt="A blue cup", size=size)
    finally:
        await generator.close()

    submit = next(request for request in requests if request.method == "POST")
    payload = json.loads(submit.content)

    assert result.success is True
    assert payload["parameters"]["size"] == size


@pytest.mark.asyncio
async def test_wan25_keeps_legacy_text2image_protocol() -> None:
    image_url = "https://cdn.example.test/wan25.png"
    client, requests = _mock_image_client({"results": [{"url": image_url}]})
    generator = DashScopeImageGenerator(
        api_key="test-explicit-key",
        model="wan2.6-t2i",
        base_url="https://dashscope-intl.aliyuncs.com/api/v1",
    )
    generator._client = client

    try:
        result = await generator.generate(
            prompt="A blue cup",
            negative_prompt="text, logo",
            size="1024*1024",
            style="<photography>",
            n=2,
            model_override="wan2.5-t2i-preview",
        )
    finally:
        await generator.close()

    submit = next(request for request in requests if request.method == "POST")
    payload = json.loads(submit.content)

    assert result.success is True
    assert len(result.images) == 1
    assert submit.url.path == "/api/v1/services/aigc/text2image/image-synthesis"
    assert payload == {
        "model": "wan2.5-t2i-preview",
        "input": {
            "prompt": "A blue cup",
            "negative_prompt": "text, logo",
        },
        "parameters": {
            "size": "1024*1024",
            "n": 2,
            "style": "<photography>",
        },
    }
