"""Typed provider capability adapters.

Only this module owns provider reasoning wire fields.  Model-specific option
sets live in capability profiles; adapters merely serialize validated settings.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from ai_gateway_core.models import ResolvedReasoningOption, resolve_reasoning_option


class CapabilityAdapterError(ValueError):
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


def _claim_path(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = body
    for key in path[:-1]:
        current = target.get(key)
        if current is None:
            current = {}
            target[key] = current
        if not isinstance(current, dict):
            raise CapabilityAdapterError(f"capability path conflicts at {'.'.join(path)}")
        target = current
    leaf = path[-1]
    if leaf in target:
        raise CapabilityAdapterError(f"capability path already owned: {'.'.join(path)}")
    target[leaf] = copy.deepcopy(value)


def apply_reasoning_adapter(
    body: dict[str, Any],
    profile: Mapping[str, Any],
    requested_option: str | None,
) -> ResolvedReasoningOption:
    resolved = resolve_reasoning_option(profile, requested_option)
    settings = resolved.settings
    adapter_id = resolved.adapter_id

    if adapter_id == "reasoning/none-v1":
        return resolved
    if adapter_id == "reasoning/dashscope-thinking-v1":
        enabled = bool(settings["enabled"])
        _claim_path(body, ("enable_thinking",), enabled)
        budget = settings.get("budget_tokens")
        if enabled and budget is not None:
            _claim_path(body, ("thinking_budget",), int(budget))
        if enabled:
            # DashScope rejects (or runs unbounded) hybrid-thinking requests
            # without an output token cap. Restore the historical floor when
            # the caller did not supply one. Responses-v1 bodies carry
            # ``input`` and use ``max_output_tokens``; chat completions use
            # ``max_tokens``.
            token_field = "max_output_tokens" if "input" in body else "max_tokens"
            if body.get(token_field) is None:
                body[token_field] = 16384
        return resolved
    if adapter_id == "reasoning/deepseek-thinking-effort-v1":
        enabled = bool(settings["enabled"])
        if enabled:
            body.pop("temperature", None)
            body.pop("top_p", None)
        _claim_path(body, ("thinking",), {"type": "enabled" if enabled else "disabled"})
        if enabled and settings.get("effort"):
            _claim_path(body, ("reasoning_effort",), settings["effort"])
        return resolved
    if adapter_id == "reasoning/anthropic-adaptive-v1":
        enabled = bool(settings["enabled"])
        if enabled:
            body.pop("temperature", None)
        _claim_path(body, ("thinking",), {"type": "adaptive" if enabled else "disabled"})
        if enabled and settings.get("effort"):
            _claim_path(body, ("output_config",), {"effort": settings["effort"]})
        return resolved
    if adapter_id == "reasoning/anthropic-budget-v1":
        enabled = bool(settings["enabled"])
        if enabled:
            body.pop("temperature", None)
            _claim_path(
                body,
                ("thinking",),
                {"type": "enabled", "budget_tokens": int(settings["budget_tokens"])},
            )
        else:
            _claim_path(body, ("thinking",), {"type": "disabled"})
        return resolved
    if adapter_id == "reasoning/openai-responses-effort-v1":
        effort = str(settings["effort"])
        if "input" in body:
            _claim_path(body, ("reasoning",), {"effort": effort, "summary": "auto"})
        else:
            _claim_path(body, ("reasoning_effort",), effort)
        return resolved
    if adapter_id == "reasoning/gemini-thinking-v1":
        thinking: dict[str, Any] = {}
        if settings.get("level") is not None:
            thinking["thinkingLevel"] = str(settings["level"]).upper()
        if settings.get("budget_tokens") is not None:
            thinking["thinkingBudget"] = int(settings["budget_tokens"])
        if settings.get("include_thoughts") is not None:
            thinking["includeThoughts"] = bool(settings["include_thoughts"])
        if thinking:
            _claim_path(body, ("generationConfig", "thinkingConfig"), thinking)
        return resolved
    if adapter_id == "reasoning/xai-effort-v1":
        effort = str(settings["effort"])
        if "input" in body:
            _claim_path(body, ("reasoning",), {"effort": effort})
        else:
            _claim_path(body, ("reasoning_effort",), effort)
        return resolved
    if adapter_id == "reasoning/openai-compatible-binary-v1":
        _claim_path(body, ("enable_thinking",), bool(settings["enabled"]))
        return resolved
    raise CapabilityAdapterError(f"unsupported reasoning adapter: {adapter_id}")


def apply_prompt_cache_adapter(body: dict[str, Any], profile: Mapping[str, Any]) -> None:
    section = profile.get("prompt_cache")
    if not isinstance(section, Mapping):
        return
    adapter_id = str(section.get("adapter_id") or "")
    if adapter_id in {"cache/none-v1", "cache/automatic-v1"}:
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
]
