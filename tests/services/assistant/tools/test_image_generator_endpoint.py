from __future__ import annotations

import pytest
from assistant_service.core.tools.image_generator_tool import DashScopeImageGenerator


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
        "services/aigc/text2image/image-synthesis"
    )
    assert generator.TASK_URL.format(task_id="task-123") == (
        "https://dashscope-intl.aliyuncs.com/api/v1/tasks/task-123"
    )


def test_explicit_image_key_uses_native_default_base() -> None:
    generator = DashScopeImageGenerator(api_key="test-explicit-key")

    assert generator.SUBMIT_URL == (
        "https://dashscope.aliyuncs.com/api/v1/"
        "services/aigc/text2image/image-synthesis"
    )
    assert generator.TASK_URL.format(task_id="task-123") == (
        "https://dashscope.aliyuncs.com/api/v1/tasks/task-123"
    )
