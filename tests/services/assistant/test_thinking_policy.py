from ai_gateway_core.enums import ModelProvider
from assistant_service.core.models.model_catalog import should_use_native_search
from assistant_service.core.models.model_registry import ModelRegistry
from assistant_service.core.models.responses_api import ChatMessage
from assistant_service.core.models.thinking_policy import (
    apply_qwen_thinking_fields,
    normalize_thinking_level,
    resolve_session_thinking_level,
    resolve_turn_thinking_level,
    session_thinking_persist_value,
)


def test_normalize_thinking_aliases_and_unknown_are_safe() -> None:
    assert normalize_thinking_level(None) == "low"
    assert normalize_thinking_level("enabled") == "low"
    assert normalize_thinking_level("not-a-level") == "low"
    assert normalize_thinking_level("HIGH") == "high"


def test_loop_keeps_session_level_across_model_turns() -> None:
    assert resolve_turn_thinking_level(requested="off", iteration=1) == "off"
    assert resolve_turn_thinking_level(requested="off", iteration=2) == "off"
    assert resolve_turn_thinking_level(requested="high", iteration=2) == "high"


def test_session_thinking_prefers_request_then_stored_then_low() -> None:
    assert resolve_session_thinking_level(requested=None, stored=None) == "low"
    assert resolve_session_thinking_level(requested=None, stored="low") == "low"
    assert resolve_session_thinking_level(requested="high", stored="low") == "high"
    assert resolve_session_thinking_level(requested="  ", stored="medium") == "medium"


def test_session_thinking_persists_only_when_request_changes_stored() -> None:
    assert (
        session_thinking_persist_value(requested=None, stored="low", effective="low") is None
    )
    assert (
        session_thinking_persist_value(requested="low", stored="low", effective="low") is None
    )
    assert (
        session_thinking_persist_value(requested="high", stored="low", effective="high")
        == "high"
    )


def test_native_search_ignores_message_text() -> None:
    assert should_use_native_search("search the latest news 今天", enabled=False) is False
    assert should_use_native_search("hello", enabled=True) is True


def test_qwen_omitted_thinking_level_defaults_to_low() -> None:
    registry = ModelRegistry(use_default_models=False)
    registry.configure_provider(ModelProvider.DASHSCOPE, api_key="test-key")
    body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="hello")],
        stream=True,
    )
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 256


def test_qwen_low_thinking_sends_budget() -> None:
    body: dict[str, object] = {}
    apply_qwen_thinking_fields(body, "qwen3.7-plus", "low", token_field="max_tokens")
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 256
    assert body["max_tokens"] == 16384
