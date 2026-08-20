from __future__ import annotations

import pytest
from ai_gateway_core.models import get_builtin_model_capabilities
from assistant_service.core.models.capability_adapters import (
    CapabilityAdapterError,
    apply_model_capability_adapters,
)


def _profile(provider: str, model: str) -> dict:
    value = get_builtin_model_capabilities(provider, model)
    assert value is not None
    return value


def test_dashscope_profile_owns_toggle_budget_and_cache_paths() -> None:
    body = {
        "model": "configured-model",
        "messages": [{"role": "system", "content": "stable"}],
    }

    resolved = apply_model_capability_adapters(
        body,
        _profile("dashscope", "qwen3.7-plus"),
        "low",
    )

    assert resolved.effective == "low"
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 128
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


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
    body = {"enable_thinking": True, "messages": []}

    with pytest.raises(CapabilityAdapterError, match="already owned"):
        apply_model_capability_adapters(
            body,
            _profile("dashscope", "qwen3.7-plus"),
            "off",
        )


def test_dashscope_thinking_floors_chat_max_tokens_when_caller_omits_it() -> None:
    body = {"model": "configured-model", "messages": []}

    apply_model_capability_adapters(body, _profile("dashscope", "qwen3.7-plus"), "low")

    assert body["enable_thinking"] is True
    assert body["max_tokens"] == 16384
    assert "max_output_tokens" not in body


def test_dashscope_thinking_floors_responses_v1_max_output_tokens() -> None:
    # Responses-v1 bodies carry ``input`` and cap output via max_output_tokens.
    body = {"model": "configured-model", "input": []}

    apply_model_capability_adapters(body, _profile("dashscope", "qwen3.7-plus"), "low")

    assert body["max_output_tokens"] == 16384
    assert "max_tokens" not in body


def test_dashscope_thinking_preserves_caller_supplied_token_caps() -> None:
    chat_body = {"messages": [], "max_tokens": 4096}
    apply_model_capability_adapters(chat_body, _profile("dashscope", "qwen3.7-plus"), "low")
    assert chat_body["max_tokens"] == 4096

    responses_body = {"input": [], "max_output_tokens": 2048}
    apply_model_capability_adapters(
        responses_body, _profile("dashscope", "qwen3.7-plus"), "low"
    )
    assert responses_body["max_output_tokens"] == 2048


def test_dashscope_thinking_off_does_not_force_a_token_cap() -> None:
    body = {"messages": []}

    apply_model_capability_adapters(body, _profile("dashscope", "qwen3.7-plus"), "off")

    assert body["enable_thinking"] is False
    assert "max_tokens" not in body
    assert "max_output_tokens" not in body
