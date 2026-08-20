from ai_gateway_core.enums import ModelProvider
from assistant_service.core.models.model_catalog import should_use_native_search
from assistant_service.core.models.model_registry import ModelRegistry
from assistant_service.core.models.responses_api import ChatMessage
from assistant_service.core.models.thinking_policy import (
    normalize_thinking_level,
    resolve_session_thinking_level,
    resolve_turn_thinking_level,
    session_thinking_persist_value,
)


def test_normalize_thinking_aliases_and_unknown_are_safe() -> None:
    assert normalize_thinking_level(None) == "auto"
    assert normalize_thinking_level("enabled") == "auto"
    assert normalize_thinking_level("not a level") == "auto"
    assert normalize_thinking_level("custom_deep") == "custom_deep"
    assert normalize_thinking_level("HIGH") == "high"


def test_loop_keeps_session_level_across_model_turns() -> None:
    assert resolve_turn_thinking_level(requested="off", iteration=1) == "off"
    assert resolve_turn_thinking_level(requested="off", iteration=2) == "off"
    assert resolve_turn_thinking_level(requested="high", iteration=2) == "high"


def test_session_thinking_prefers_request_then_stored_then_auto() -> None:
    assert resolve_session_thinking_level(requested=None, stored=None) == "auto"
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


def test_qwen_omitted_reasoning_option_uses_catalog_default() -> None:
    registry = ModelRegistry()
    body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="hello")],
        stream=True,
    )
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 128


def test_qwen_low_reasoning_budget_comes_from_catalog_profile() -> None:
    registry = ModelRegistry()
    body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="hello")],
        stream=True,
        thinking_level="low",
    )
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 128
