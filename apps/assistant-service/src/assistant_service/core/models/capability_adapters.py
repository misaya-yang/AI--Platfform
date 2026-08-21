"""Typed provider capability adapters.

Only this module owns provider reasoning wire fields.  Model-specific option
sets live in capability profiles; adapters merely serialize validated settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_gateway_core.models import (
    ReasoningWireError,
    ResolvedReasoningOption,
    apply_reasoning_wire,
)


class CapabilityAdapterError(ReasoningWireError):
    """A capability profile cannot be represented on the selected wire API."""


def native_search_config_from_profile(profile: Mapping[str, Any]) -> dict[str, Any] | None:
    section = profile.get("native_search")
    if not isinstance(section, Mapping) or not section.get("enabled"):
        return None
    adapter_id = str(section.get("adapter_id") or "")
    config = dict(section.get("config") or {})
    if adapter_id == "search/dashscope-native-v1":
        return {"enable_search": True}
    if adapter_id == "search/anthropic-server-tool-v1":
        return {"tool_type": "web_search_20250305", "max_uses": config.get("max_uses", 5)}
    if adapter_id == "search/gemini-google-search-v1":
        return {"tool_type": "google_search"}
    if adapter_id == "search/openai-web-search-v1":
        return {"tool_type": "web_search"}
    if adapter_id == "search/none-v1":
        return None
    raise CapabilityAdapterError(f"unsupported native search adapter: {adapter_id}")


def apply_reasoning_adapter(
    body: dict[str, Any],
    profile: Mapping[str, Any],
    requested_option: str | None,
) -> ResolvedReasoningOption:
    try:
        return apply_reasoning_wire(body, profile, requested_option)
    except ReasoningWireError as exc:
        raise CapabilityAdapterError(str(exc)) from exc


def apply_prompt_cache_adapter(body: dict[str, Any], profile: Mapping[str, Any]) -> None:
    section = profile.get("prompt_cache")
    if not isinstance(section, Mapping):
        return
    adapter_id = str(section.get("adapter_id") or "")
    if adapter_id in {
        "cache/none-v1",
        "cache/automatic-v1",
        "cache/dashscope-session-v1",
    }:
        return
    if adapter_id == "cache/anthropic-breakpoint-v1":
        system = body.get("system")
        if isinstance(system, str) and system:
            from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

            if CACHE_SPLIT_MARKER in system:
                static_prefix, dynamic_tail = system.split(CACHE_SPLIT_MARKER, 1)
                blocks = [
                    {
                        "type": "text",
                        "text": static_prefix.rstrip(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if dynamic_tail.strip():
                    blocks.append(
                        {
                            "type": "text",
                            "text": dynamic_tail.lstrip(),
                            "cache_control": {"type": "ephemeral"},
                        }
                    )
                body["system"] = blocks
            else:
                body["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
        tools = body.get("tools")
        if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
            if "cache_control" in tools[-1]:
                raise CapabilityAdapterError("tool cache_control path already owned")
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return
    if adapter_id != "cache/openai-content-breakpoint-v1":
        raise CapabilityAdapterError(f"unsupported prompt cache adapter: {adapter_id}")
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                if "cache_control" in last:
                    raise CapabilityAdapterError("cache_control path already owned")
                last["cache_control"] = {"type": "ephemeral"}
        return


def request_headers_from_profile(profile: Mapping[str, Any]) -> dict[str, str]:
    """Return validated provider headers owned by capability adapters."""

    section = profile.get("prompt_cache")
    if not isinstance(section, Mapping):
        return {}
    adapter_id = str(section.get("adapter_id") or "")
    config = section.get("config")
    if (
        adapter_id == "cache/dashscope-session-v1"
        and isinstance(config, Mapping)
        and config.get("enabled") is True
    ):
        return {"x-dashscope-session-cache": "enable"}
    return {}


def apply_model_capability_adapters(
    body: dict[str, Any],
    profile: Mapping[str, Any],
    requested_option: str | None,
) -> ResolvedReasoningOption:
    resolved = apply_reasoning_adapter(body, profile, requested_option)
    apply_prompt_cache_adapter(body, profile)
    if body.get("enable_thinking") is False and "thinking_budget" in body:
        raise CapabilityAdapterError("disabled thinking cannot carry a thinking budget")
    return resolved


__all__ = [
    "CapabilityAdapterError",
    "apply_model_capability_adapters",
    "apply_prompt_cache_adapter",
    "apply_reasoning_adapter",
    "native_search_config_from_profile",
    "request_headers_from_profile",
]
