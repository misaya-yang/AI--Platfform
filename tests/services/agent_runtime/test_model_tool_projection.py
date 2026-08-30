from __future__ import annotations

import copy

from ai_gateway_core.models import get_builtin_model_capabilities

from src.services.agent_runtime.model_plane import (
    _chat_tools_from_runtime,
    _native_responses_body,
)


def _profile() -> dict:
    profile = get_builtin_model_capabilities("dashscope", "qwen3.7-plus")
    assert profile is not None
    return profile


def _native_search_profile() -> dict:
    profile = copy.deepcopy(_profile())
    profile["native_search"] = {
        "adapter_id": "search/dashscope-native-v1",
        "enabled": True,
        "config": {},
    }
    profile["tools"]["web_search_wire"] = "native"
    return profile


def test_chat_completions_flattens_namespaces_and_omits_native_search() -> None:
    tools = _chat_tools_from_runtime(
        [
            {
                "type": "function",
                "name": "lookup",
                "description": "Look up one value.",
                "parameters": {"type": "object", "properties": {}},
            },
            {"type": "web_search"},
            {
                "type": "namespace",
                "name": "mcp",
                "tools": [
                    {
                        "type": "function",
                        "name": "write",
                        "description": "Write through MCP.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
        ],
        _native_search_profile(),
        allowed_tool_names={"lookup", "write"},
    )

    assert [tool["function"]["name"] for tool in tools] == ["lookup", "write"]


def test_native_tool_choice_none_omits_runtime_tools_on_first_call() -> None:
    body, _aliases = _native_responses_body(
        {
            "input": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up one value.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=_native_search_profile(),
        reasoning_option="minimal",
        tool_choice="none",
    )

    assert "tools" not in body
    assert "tool_choice" not in body
