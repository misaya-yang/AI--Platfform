from __future__ import annotations

import pytest
from ai_gateway_core.models import get_builtin_model_capabilities
from assistant_service.core.models.capability_adapters import (
    CapabilityAdapterError,
    apply_model_capability_adapters,
    request_headers_from_profile,
)


def _profile(provider: str, model: str) -> dict:
    value = get_builtin_model_capabilities(provider, model)
    assert value is not None
    return value


def test_dashscope_profile_owns_responses_effort_path() -> None:
    body = {
        "model": "configured-model",
        "input": [{"role": "system", "content": "stable"}],
    }

    resolved = apply_model_capability_adapters(
        body,
        _profile("dashscope", "qwen3.7-plus"),
        "low",
    )

    assert resolved.effective == "minimal"
    assert body["reasoning"] == {"effort": "minimal"}
    assert "enable_thinking" not in body
    assert "thinking_budget" not in body
    assert request_headers_from_profile(_profile("dashscope", "qwen3.7-plus")) == {
        "x-dashscope-session-cache": "enable"
    }


def test_deepseek_profile_removes_sampling_and_maps_alias_to_real_effort() -> None:
    body = {"messages": [], "temperature": 0.2, "top_p": 0.8}

    resolved = apply_model_capability_adapters(
        body,
        _profile("deepseek", "deepseek-v4-pro"),
        "medium",
    )

    assert resolved.effective == "high"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body
    assert "top_p" not in body


def test_gemini_profile_emits_one_thinking_config() -> None:
    body = {"generationConfig": {"maxOutputTokens": 2048}}

    apply_model_capability_adapters(
        body,
        _profile("google", "gemini-3.1-pro-preview"),
        "low",
    )

    assert body["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "LOW",
        "includeThoughts": True,
    }


def test_existing_owned_reasoning_path_is_rejected_before_http() -> None:
    body = {"reasoning": {"effort": "high"}, "input": []}

    with pytest.raises(CapabilityAdapterError, match="already owned"):
        apply_model_capability_adapters(
            body,
            _profile("dashscope", "qwen3.7-plus"),
            "off",
        )


def test_dashscope_responses_profile_keeps_explicit_chat_fallback() -> None:
    body = {"model": "configured-model", "messages": []}

    apply_model_capability_adapters(
        body,
        _profile("dashscope", "qwen3.7-plus"),
        "low",
    )

    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 128


def test_dashscope_responses_effort_does_not_invent_a_token_cap() -> None:
    body = {"model": "configured-model", "input": []}

    apply_model_capability_adapters(body, _profile("dashscope", "qwen3.7-plus"), "low")

    assert body["reasoning"] == {"effort": "minimal"}
    assert "max_output_tokens" not in body
    assert "max_tokens" not in body


def test_dashscope_responses_effort_preserves_caller_supplied_token_cap() -> None:
    responses_body = {"input": [], "max_output_tokens": 2048}
    apply_model_capability_adapters(responses_body, _profile("dashscope", "qwen3.7-plus"), "low")
    assert responses_body["max_output_tokens"] == 2048


def test_dashscope_reasoning_off_uses_native_none_effort() -> None:
    body = {"input": []}

    apply_model_capability_adapters(body, _profile("dashscope", "qwen3.7-plus"), "off")

    assert body["reasoning"] == {"effort": "none"}
    assert "max_tokens" not in body
    assert "max_output_tokens" not in body
