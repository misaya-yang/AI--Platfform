"""
Model Registry - Unified interface for multiple LLM providers.

Supports (default catalog as of 2026-04):
- OpenAI (gpt-4o, o1)
- Anthropic (claude-opus-4-5, claude-sonnet-4-5)
- DeepSeek (deepseek-chat, deepseek-reasoner)
- DashScope/Qwen (qwen3.7-plus, qwen3.6-plus, qwen-max)
- Google / Google Vertex (gemini-3-pro-preview, gemini-3-flash-preview)
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from ai_gateway_core.enums import ModelAccessLevel, ModelProvider
from ai_gateway_core.logging import get_logger, record_internal_exception
from ai_gateway_core.models import ChatMessage
from ai_gateway_core.models import normalize_chat_message as _normalize_message

from ..quality.cache_optimizer import normalize_provider_cache_usage
from .model_catalog import (
    DEFAULT_MODELS as DEFAULT_MODELS,
)
from .model_catalog import (
    NATIVE_SEARCH_CAPABLE as NATIVE_SEARCH_CAPABLE,
)
from .model_catalog import (
    ModelConfig as ModelConfig,
)
from .model_catalog import (
    ModelInfo as ModelInfo,
)
from .model_catalog import (
    should_use_native_search as should_use_native_search,
)
from .provider_errors import (
    _SAFE_ANTHROPIC_ERROR_TYPES,
    _SAFE_ANTHROPIC_STOP_REASONS,
    _SAFE_GOOGLE_BLOCK_REASONS,
    _SAFE_GOOGLE_FINISH_REASONS,
    _SAFE_OPENAI_ERROR_TYPES,
    _SAFE_OPENAI_FINISH_REASONS,
)
from .provider_errors import (
    ProviderStreamError as ProviderStreamError,
)
from .registry_lifecycle import RegistryLifecycleMixin
from .request_safety import (
    _raise_for_status_without_query_secrets,
    _safe_request_error,
)
from .request_safety import (
    _request_without_query_secrets as _request_without_query_secrets,
)
from .responses_api import (
    ResponsesAPIError,
    build_responses_request,
    iter_responses_stream,
    parse_responses_response,
)

# Re-export so existing ``from ...model_registry import ModelProvider`` sites
# keep working. Phase 5d moved the enum definitions to ``ai_gateway_core``
# so gateway routes (health, assistant) can import the enum without pulling
# in the full registry. Delete re-export once AS-internal call sites migrate.
__all__ = ["ModelAccessLevel", "ModelProvider", "ModelInfo", "ModelRegistry"]

logger = get_logger(__name__)


def _sanitize_usage(raw_usage: dict[str, Any]) -> dict[str, int]:
    """
    Sanitize and normalize usage dict.

    - Only include integer values and known cache-token fields
    - Normalize OpenAI keys (prompt_tokens -> input_tokens, completion_tokens -> output_tokens)

    Some providers (e.g., DashScope) return nested dicts like:
    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
    """
    return normalize_provider_cache_usage(raw_usage)


@contextlib.asynccontextmanager
async def _safe_provider_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    body: dict[str, Any],
) -> AsyncIterator[Any]:
    """Open a provider stream without retaining credentials or response bodies."""

    safe_transport_error: httpx.RequestError | None = None
    try:
        async with client.stream("POST", endpoint, json=body) as response:
            _raise_for_status_without_query_secrets(response)
            yield response
    except httpx.HTTPStatusError:
        raise
    except httpx.RequestError as exc:
        safe_transport_error = _safe_request_error(exc)
    if safe_transport_error is not None:
        raise safe_transport_error


def _parse_sse_event(data: str, *, provider: str) -> dict[str, Any]:
    """Parse one SSE payload without retaining malformed provider data in an exception."""

    invalid_json = False
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        invalid_json = True
        event = None
    if invalid_json:
        raise ProviderStreamError(provider, "invalid_sse_json")
    if not isinstance(event, dict):
        raise ProviderStreamError(provider, "invalid_event")
    return event


def _validate_openai_tool_call_deltas(
    raw_calls: Any,
    *,
    established_calls: dict[int, tuple[str, str]] | None = None,
) -> list[dict[str, Any]] | None:
    """Validate and normalize partial tool calls before executor exposure.

    Some OpenAI-compatible providers repeat ``id: \"\"`` (and occasionally an
    empty function name) on argument-only continuation frames.  Those empty
    identity fields are safe to discard only after a prior frame established
    the same tool-call index with a non-empty ID.
    """

    if raw_calls is None:
        return None
    if not isinstance(raw_calls, list):
        raise ProviderStreamError("openai-compatible", "invalid_event")
    validated: list[dict[str, Any]] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict) or not raw_call:
            raise ProviderStreamError("openai-compatible", "invalid_event")
        call = dict(raw_call)
        index: int | None = None
        if "index" in call:
            raw_index = call["index"]
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderStreamError("openai-compatible", "invalid_event")
            index = raw_index
        established_identity = (
            established_calls.get(index)
            if index is not None and established_calls is not None
            else None
        )
        is_continuation = established_identity is not None
        if "id" in call:
            if not isinstance(call["id"], str):
                raise ProviderStreamError("openai-compatible", "invalid_event")
            if not call["id"]:
                if not is_continuation:
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                call.pop("id")
            elif is_continuation and call["id"] != established_identity[0]:
                raise ProviderStreamError("openai-compatible", "invalid_event")
        elif index is not None and not is_continuation:
            raise ProviderStreamError("openai-compatible", "invalid_event")
        if "type" in call and call["type"] != "function":
            raise ProviderStreamError("openai-compatible", "invalid_event")
        function = call.get("function")
        if function is not None:
            if (
                not isinstance(function, dict)
                or not function
                or not ({"name", "arguments"} & set(function))
            ):
                raise ProviderStreamError("openai-compatible", "invalid_event")
            function = dict(function)
            if "name" in function:
                if not isinstance(function["name"], str):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                if not function["name"]:
                    if not is_continuation:
                        raise ProviderStreamError("openai-compatible", "invalid_event")
                    function.pop("name")
                elif is_continuation and function["name"] != established_identity[1]:
                    raise ProviderStreamError("openai-compatible", "invalid_event")
            if "arguments" in function and not isinstance(function["arguments"], str):
                raise ProviderStreamError("openai-compatible", "invalid_event")
            if not function:
                raise ProviderStreamError("openai-compatible", "invalid_event")
            call["function"] = function
        elif "id" not in call:
            raise ProviderStreamError("openai-compatible", "invalid_event")
        if index is not None and not is_continuation:
            function_name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(function_name, str) or not function_name:
                raise ProviderStreamError("openai-compatible", "invalid_event")
            if established_calls is not None:
                if any(identity[0] == call["id"] for identity in established_calls.values()):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                established_calls[index] = (call["id"], function_name)
        validated.append(call)
    return validated


# --- Streaming smoother for Vertex-style chunked upstreams ---
# Vertex Express Mode flushes SSE frames ~1/sec with ~100 chars each — the
# frontend then renders those as 3-6 big jumps and the user perceives it as
# "not streaming." We split each Vertex frame into smaller sub-deltas with
# a small inter-chunk delay so the UI sees a token-like cadence without
# materially changing total stream time.
#
# Do not apply this to OpenAI-compatible streams (DashScope, DeepSeek, OpenAI).
# Splitting an already-received provider frame and sleeping between four-character
# slices is presentation work, not model streaming. It delayed delivery by roughly
# 20 ms per slice and added tens of seconds to long answers in live measurements.
# OpenAI-compatible frames are forwarded immediately and verbatim.
#
# Operator override: ``GEMINI_SMOOTHER_DISABLED=1`` turns this off for Google
# providers (the provider then yields each upstream frame verbatim).
_SMOOTHER_DISABLED = os.environ.get("GEMINI_SMOOTHER_DISABLED", "").lower() in {"1", "true", "yes"}


def configure_stream_smoother(*, disabled: bool) -> None:
    """Freeze the process smoother switch from the startup snapshot."""

    global _SMOOTHER_DISABLED
    _SMOOTHER_DISABLED = bool(disabled)
_SMOOTHER_CHARS_PER_CHUNK = 4
_SMOOTHER_DELAY_SECONDS = 0.020
_SMOOTHER_MIN_TEXT_LEN = 12  # chunks smaller than this don't benefit from splitting


async def _smooth_text_delta(text: str) -> AsyncIterator[str]:
    """Split a large Vertex text frame into smaller sub-deltas.

    For a 200-char Vertex frame this yields ~50 sub-chunks at ~20ms intervals,
    which the frontend renders as ~25 char/s typewriter flow — noticeably
    streamy, total extra latency ~1s (well below the original 1-second
    gap between Vertex frames so no net regression on total time).
    """
    if _SMOOTHER_DISABLED or len(text) <= _SMOOTHER_MIN_TEXT_LEN:
        yield text
        return
    import asyncio as _asyncio

    chunk_size = _SMOOTHER_CHARS_PER_CHUNK
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]
        # Skip sleep on the final slice — no one sees it
        if i + chunk_size < len(text):
            await _asyncio.sleep(_SMOOTHER_DELAY_SECONDS)


@dataclass
class StreamDelta:
    """A single streaming delta from the model."""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    thought_signature: str | None = None  # Gemini 3 thought signature
    thinking_content: str | None = None  # Qwen reasoning_content / Gemini thought parts
    # Complete provider-native assistant blocks emitted once the message is
    # closed. Used only when the provider requires verbatim continuation.
    provider_content_blocks: list[dict[str, Any]] | None = None


class ModelRegistry(RegistryLifecycleMixin):
    """Unified model catalog and provider request interface."""

    def _build_request_body(
        self,
        provider: ModelProvider,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
        native_search_config: dict[str, Any] | None = None,
        openai_local_runtime: Any | None = None,
    ) -> dict[str, Any]:
        """Build request body for the provider's API."""
        if self._uses_responses_v1(provider):
            reasoning_effort = (
                thinking_level
                if provider == ModelProvider.OPENAI
                and thinking_level in {"minimal", "low", "medium", "high"}
                else None
            )
            body = build_responses_request(
                model_id=model_id,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_tokens,
                tools=tools,
                stream=stream,
                reasoning_effort=reasoning_effort,
                local_runtime=(openai_local_runtime if provider == ModelProvider.OPENAI else None),
            )
            if provider == ModelProvider.DASHSCOPE:
                from .thinking_policy import apply_qwen_thinking_fields

                apply_qwen_thinking_fields(
                    body, model_id, thinking_level, token_field="max_output_tokens"
                )
                if native_search_config and native_search_config.get("enable_search"):
                    response_tools = body.setdefault("tools", [])
                    response_tools.append({"type": "web_search"})
            return body
        if provider == ModelProvider.ANTHROPIC:
            return self._build_anthropic_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                native_search_config=native_search_config,
            )
        elif provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX):
            return self._build_google_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                thinking_level,
                tool_config,
                native_search_config=native_search_config,
            )
        else:
            return self._build_openai_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                thinking_level=thinking_level,
                native_search_config=native_search_config,
            )

    def _build_openai_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        thinking_level: str | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build OpenAI-compatible request body."""
        from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

        formatted_messages = []
        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            content = msg.content
            # Anthropic-only cache marker — strip for OpenAI-compat providers.
            if msg.role == "system" and isinstance(content, str) and CACHE_SPLIT_MARKER in content:
                content = content.replace(CACHE_SPLIT_MARKER, "").replace("\n\n\n\n", "\n\n")
            m: dict[str, Any] = {"role": msg.role, "content": content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            # Handle vision content
            if msg.images and msg.role == "user":
                content_parts = [{"type": "text", "text": msg.content}]
                for img in msg.images:
                    if img.startswith("http") or img.startswith("data:"):
                        # URL or data URL - use as-is
                        content_parts.append({"type": "image_url", "image_url": {"url": img}})
                    else:
                        # Raw base64 - assume jpeg
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img}"},
                            }
                        )
                m["content"] = content_parts
            formatted_messages.append(m)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        # DashScope extensions are TOP-LEVEL fields on the compat endpoint —
        # NOT nested under `extra_body`. The `extra_body` dict is a client-
        # side concept in the OpenAI Python SDK (which unpacks it into the
        # request body); we POST raw JSON, so wrapping silently drops the
        # flag. Verified live against qwen3.6-plus on 2026-04-21:
        #   body.extra_body.enable_search = True → flag IGNORED, model
        #     refuses ("I can't fetch real-time data").
        #   body.enable_search = True → flag RESPECTED, model returns
        #     results with real team names and scores.
        # Same applies to enable_thinking. Off must be explicit false:
        # omitting the flag uses the provider default (on for Qwen 3.7 Plus).
        from .thinking_policy import apply_qwen_thinking_fields

        apply_qwen_thinking_fields(body, model_id, thinking_level, token_field="max_tokens")
        if native_search_config and native_search_config.get("enable_search"):
            # DashScope CN vs Intl differ in where ``enable_search`` belongs:
            #   CN (dashscope.aliyuncs.com):   body.enable_search = True (top-level)
            #     Verified 2026-04-21 — extra_body form is IGNORED on CN.
            #   Intl (dashscope-intl.aliyuncs.com):  body.extra_body.enable_search = True
            #     Verified 2026-04-23 — top-level form returns HTTP 500.
            # Also documented at https://www.alibabacloud.com/help/en/model-studio/web-search —
            # the Intl doc explicitly uses ``extra_body={"enable_search": True,
            # "search_options": {"search_strategy": "agent"}}``.
            provider = ModelProvider.DASHSCOPE
            cfg = self._configs.get(provider) if provider in self._configs else None
            cfg_base = (cfg.base_url if cfg else "") or ""
            if "-intl" in cfg_base:
                body["extra_body"] = {
                    **(body.get("extra_body") or {}),
                    "enable_search": True,
                    "search_options": {"search_strategy": "agent"},
                }
            else:
                body["enable_search"] = True
        return body

    def _build_anthropic_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Anthropic-specific request body."""
        system_prompt = None
        formatted_messages = []

        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            if msg.role == "system":
                system_prompt = msg.content
                continue

            if msg.role == "tool":
                if not msg.tool_call_id:
                    raise ValueError("Anthropic tool results require a non-empty tool_call_id")
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": str(msg.tool_call_id),
                    "content": str(msg.content or ""),
                }
                if (
                    formatted_messages
                    and formatted_messages[-1].get("role") == "user"
                    and isinstance(formatted_messages[-1].get("content"), list)
                    and all(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in formatted_messages[-1]["content"]
                    )
                ):
                    formatted_messages[-1]["content"].append(tool_result)
                else:
                    formatted_messages.append({"role": "user", "content": [tool_result]})
                continue

            if msg.role == "assistant" and msg.provider_content_blocks is not None:
                provider_blocks = msg.provider_content_blocks
                if not isinstance(provider_blocks, list) or len(provider_blocks) > 128:
                    raise ValueError("Anthropic provider content blocks are invalid")
                if any(
                    not isinstance(block, dict)
                    or not isinstance(block.get("type"), str)
                    or not block.get("type")
                    for block in provider_blocks
                ):
                    raise ValueError("Anthropic provider content blocks are invalid")
                formatted_messages.append(
                    {
                        "role": "assistant",
                        "content": copy.deepcopy(provider_blocks),
                    }
                )
                continue

            m: dict[str, Any] = {"role": msg.role}
            content_parts: list[dict[str, Any]] = []

            # Handle vision content
            if msg.images and msg.role == "user":
                for img in msg.images:
                    if img.startswith("http"):
                        # Anthropic supports URL source
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": img,
                                },
                            }
                        )
                    elif img.startswith("data:"):
                        # Parse data URL: data:{mime_type};base64,{base64_data}
                        try:
                            header, base64_data = img.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                        except (ValueError, IndexError):
                            media_type = "image/jpeg"
                            base64_data = img
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64_data,
                                },
                            }
                        )
                    else:
                        # Raw base64 - assume jpeg
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img,
                                },
                            }
                        )
            if msg.content:
                content_parts.append({"type": "text", "text": msg.content})

            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    raw_arguments = function.get("arguments") or {}
                    if isinstance(raw_arguments, str):
                        invalid_arguments = False
                        try:
                            parsed_arguments = json.loads(raw_arguments) if raw_arguments else {}
                        except json.JSONDecodeError:
                            invalid_arguments = True
                            parsed_arguments = None
                        if invalid_arguments:
                            raise ValueError("Anthropic tool arguments must be valid JSON")
                    else:
                        parsed_arguments = raw_arguments
                    if not isinstance(parsed_arguments, dict):
                        raise ValueError("Anthropic tool arguments must decode to an object")
                    tool_id = str(tool_call.get("id") or "")
                    tool_name = str(function.get("name") or "")
                    if not tool_id or not tool_name:
                        raise ValueError("Anthropic tool calls require non-empty id and name")
                    content_parts.append(
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": parsed_arguments,
                        }
                    )

            m["content"] = content_parts if content_parts else str(msg.content or "")

            formatted_messages.append(m)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,
            "stream": stream,
        }
        if system_prompt:
            # Split the prompt on CACHE_SPLIT_MARKER (inserted by
            # build_system_prompt_v2) into a tenant-stable static prefix and
            # a per-tenant/per-scenario tail. Both get `cache_control:
            # ephemeral` so the prefix caches across all tenants while the
            # tail still caches per (tenant, scenario, tools) combination.
            # Anthropic allows up to 4 cache breakpoints; we use 2.
            from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

            if CACHE_SPLIT_MARKER in system_prompt:
                static_prefix, dynamic_tail = system_prompt.split(CACHE_SPLIT_MARKER, 1)
                blocks = [
                    {
                        "type": "text",
                        "text": static_prefix.rstrip(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                dynamic_tail = dynamic_tail.lstrip()
                if dynamic_tail:
                    blocks.append(
                        {
                            "type": "text",
                            "text": dynamic_tail,
                            "cache_control": {"type": "ephemeral"},
                        }
                    )
                body["system"] = blocks
            else:
                body["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
        if tools:
            # Convert OpenAI tool format to Anthropic format.
            anthropic_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    anthropic_tools.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "input_schema": func.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
            if anthropic_tools:
                # Cache the tool definitions too — they're stable across turns
                # for the same session. Put the marker on the last tool entry;
                # Anthropic caches everything up to (and including) the marker.
                anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
                body["tools"] = anthropic_tools

        # Native search — Anthropic server tool `web_search_20250305`. Append
        # to the tools list (the model will call it internally and return
        # inline citations as `tool_use`/`tool_result` blocks we already
        # ignore in streaming; sink it to DEBUG if it shows up).
        if native_search_config and native_search_config.get("tool_type") == "web_search_20250305":
            server_tool = {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": native_search_config.get("max_uses", 5),
            }
            existing = list(body.get("tools") or [])
            existing.append(server_tool)
            body["tools"] = existing
        return body

    def _build_google_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Google Gemini API request body."""
        del stream
        contents = []
        system_instruction = None

        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            if msg.role == "system":
                system_instruction = msg.content
                continue

            # Handle tool result messages (functionResponse)
            if msg.role == "tool" and msg.tool_call_id:
                # Parse function name from tool_call_id (format: "call_<name>")
                func_name = msg.name or msg.tool_call_id
                if func_name.startswith("call_"):
                    func_name = func_name[5:]

                # Try to parse content as JSON, otherwise wrap as object
                try:
                    response_data = json.loads(msg.content) if msg.content else {}
                except json.JSONDecodeError:
                    response_data = {"result": msg.content}

                contents.append(
                    {
                        "role": "user",  # Google uses "user" role for function responses
                        "parts": [
                            {"functionResponse": {"name": func_name, "response": response_data}}
                        ],
                    }
                )
                continue

            role = "user" if msg.role == "user" else "model"
            parts = []

            # Handle assistant messages with tool_calls (functionCall)
            if msg.role == "assistant":
                # Add text content first if present
                if msg.content:
                    text_part = {"text": msg.content}
                    # Attach thoughtSignature to text part if present
                    if msg.thought_signature:
                        text_part["thoughtSignature"] = msg.thought_signature
                    parts.append(text_part)

                # Add function calls with thoughtSignature (CRITICAL for Gemini 3)
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if not isinstance(tc, dict):
                            raise ValueError("Google tool call history must be an object")
                        func = tc.get("function", {})
                        if not isinstance(func, dict):
                            raise ValueError("Google tool function history must be an object")
                        func_name = func.get("name", "")
                        if not isinstance(func_name, str) or not func_name:
                            raise ValueError("Google tool function name must be non-empty")

                        # Parse arguments
                        invalid_arguments = False
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except (json.JSONDecodeError, TypeError):
                            invalid_arguments = True
                            args = None
                        if invalid_arguments:
                            raise ValueError("Google tool arguments must be valid JSON")
                        if not isinstance(args, dict):
                            raise ValueError("Google tool arguments must be an object")

                        func_call_part: dict[str, Any] = {
                            "functionCall": {"name": func_name, "args": args}
                        }

                        # CRITICAL: Include thoughtSignature if present (required for Gemini 3)
                        if "thoughtSignature" in tc:
                            func_call_part["thoughtSignature"] = tc["thoughtSignature"]
                            name_hash = hashlib.sha256(str(func_name).encode("utf-8")).hexdigest()[
                                :10
                            ]
                            logger.debug(
                                "[GEMINI3] Including thoughtSignature name_hash=%s",
                                name_hash,
                            )

                        parts.append(func_call_part)

                # If only thoughtSignature is present without content or tool calls (unlikely but possible)
                if not msg.content and not msg.tool_calls and msg.thought_signature:
                    parts.append({"text": "", "thoughtSignature": msg.thought_signature})

                if parts:
                    contents.append({"role": role, "parts": parts})
                continue

            # Handle vision content
            if msg.images and msg.role == "user":
                for img in msg.images:
                    if img.startswith("http"):
                        # Infer mime type from URL extension
                        mime_type = "image/jpeg"
                        if ".png" in img.lower():
                            mime_type = "image/png"
                        elif ".gif" in img.lower():
                            mime_type = "image/gif"
                        elif ".webp" in img.lower():
                            mime_type = "image/webp"
                        parts.append({"fileData": {"fileUri": img, "mimeType": mime_type}})
                    elif img.startswith("data:"):
                        # Parse data URL: data:{mime_type};base64,{base64_data}
                        try:
                            header, base64_data = img.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                        except (ValueError, IndexError):
                            mime_type = "image/jpeg"
                            base64_data = img
                        # Gemini REST API uses camelCase
                        parts.append({"inlineData": {"mimeType": mime_type, "data": base64_data}})
                    else:
                        # Raw base64 data, assume jpeg
                        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": img}})
                parts.append({"text": msg.content})
            else:
                parts.append({"text": msg.content})

            contents.append({"role": role, "parts": parts})

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens or 8192,
            },
        }

        # Thinking configuration.
        #
        # Gemini 2.5+ / 3.x only emits "thought summary" parts
        # (`candidates[].content.parts[].thought == true`) when the request
        # body explicitly enables `thinkingConfig.includeThoughts`. Without
        # it the REST API silently drops thinking content, which breaks the
        # Activity drawer (no thinking_start / thinking_delta SSE events).
        #
        # Rules:
        #   - When the caller explicitly sets `thinking_level`, honour it
        #     (PPT request path) AND turn on includeThoughts so thought
        #     summaries still stream to the Activity drawer.
        #   - Otherwise, default to `includeThoughts: true` for Gemini
        #     models that support thought summaries (2.5+ / 3.x). We skip
        #     older ids (gemini-1.5-*, gemini-pro, etc.) since their REST
        #     surface does not accept the field.
        mid = (model_id or "").lower()
        supports_thought_summaries = "gemini-2.5" in mid or "gemini-3" in mid
        from .thinking_policy import normalize_thinking_level

        effective_level = normalize_thinking_level(thinking_level)
        if effective_level != "off" and supports_thought_summaries:
            gemini_level = "HIGH" if effective_level == "high" else effective_level.upper()
            body["generationConfig"]["thinkingConfig"] = {
                "thinkingLevel": gemini_level,
                "includeThoughts": True,
            }

        if system_instruction:
            # Strip Anthropic-only cache marker before sending to Gemini.
            from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

            if isinstance(system_instruction, str) and CACHE_SPLIT_MARKER in system_instruction:
                system_instruction = system_instruction.replace(CACHE_SPLIT_MARKER, "").replace(
                    "\n\n\n\n", "\n\n"
                )
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        if tools:
            # Convert OpenAI tool format to Google format
            google_tools = []
            function_declarations = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    function_declarations.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "parameters": func.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
            if function_declarations:
                google_tools.append({"functionDeclarations": function_declarations})
                body["tools"] = google_tools

        # Native search — Gemini 3.x supports combining built-in grounding
        # (`google_search`) with `functionDeclarations` in a single request,
        # so always append. Older Gemini 1.5 / 2.0 used to 400 on this combo;
        # we no longer ship those models. The capability map tells us which
        # form to emit (`google_search` vs the legacy `google_search_retrieval`).
        if native_search_config and native_search_config.get("tool_type") in (
            "google_search",
            "google_search_retrieval",
        ):
            search_tool_key = native_search_config["tool_type"]
            existing = list(body.get("tools") or [])
            existing.append({search_tool_key: {}})
            body["tools"] = existing

        # Apply tool_config if provided
        if tool_config:
            body["toolConfig"] = tool_config

        return body

    async def chat(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
        native_search_config: dict[str, Any] | None = None,
        openai_local_runtime: Any | None = None,
    ) -> tuple[str, dict[str, int]]:
        """
        Non-streaming chat completion.

        Returns:
            Tuple of (response_content, usage_dict)
        """
        model = self.get_model(model_id)
        if not model:
            raise ValueError(f"Unknown model: {model_id}")

        client = await self._get_client(model.provider, model_id=model_id)
        # Google providers return ephemeral clients from _get_client;
        # wrap the call so the client is closed even on exception.
        _owns_client = model.provider in (
            ModelProvider.GOOGLE,
            ModelProvider.GOOGLE_VERTEX,
        )
        body = self._build_request_body(
            model.provider,
            model_id,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=False,
            thinking_level=thinking_level,
            native_search_config=native_search_config,
            openai_local_runtime=openai_local_runtime,
        )

        if model.provider == ModelProvider.GOOGLE:
            # Path differs between AI Studio and Vertex; auth stays in headers.
            endpoint = self._google_endpoint(model_id, stream=False)
        elif model.provider == ModelProvider.GOOGLE_VERTEX:
            endpoint = self._vertex_endpoint(model_id, stream=False)
        elif model.provider == ModelProvider.ANTHROPIC:
            endpoint = "/v1/messages"
        elif self._uses_responses_v1(model.provider):
            endpoint = self._responses_endpoint(model.provider)
        else:
            endpoint = "/v1/chat/completions"

        safe_transport_error: httpx.RequestError | None = None
        try:
            try:
                response = await client.post(endpoint, json=body)
            except httpx.RequestError as exc:
                safe_transport_error = _safe_request_error(exc)
            if safe_transport_error is not None:
                raise safe_transport_error
            _raise_for_status_without_query_secrets(response)
            invalid_response = False
            try:
                data = response.json()
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.models.model_registry.internal_failure", exc
                )
                invalid_response = True
                data = None
            if invalid_response or not isinstance(data, dict):
                provider_label = (
                    "anthropic"
                    if model.provider == ModelProvider.ANTHROPIC
                    else (
                        "google"
                        if model.provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX)
                        else "openai-compatible"
                    )
                )
                raise ProviderStreamError(provider_label, "invalid_response_json")
        finally:
            if _owns_client:
                await client.aclose()

        if model.provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX):
            # Parse Google Gemini response
            content = ""
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "text" in part:
                        content += part["text"]
            usage = _sanitize_usage(data.get("usageMetadata", {}))
        elif model.provider == ModelProvider.ANTHROPIC:
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            usage = _sanitize_usage(data.get("usage", {}))
        elif self._uses_responses_v1(model.provider):
            result = parse_responses_response(data)
            if result.tool_calls:
                raise ResponsesAPIError("nonstream_tool_calls_unsupported")
            content = result.content
            usage = result.usage
        else:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = _sanitize_usage(data.get("usage", {}))

        return content, usage

    async def chat_stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
        native_search_config: dict[str, Any] | None = None,
        openai_local_runtime: Any | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """
        Streaming chat completion.

        Yields StreamDelta objects with incremental content.
        """
        model = self.get_model(model_id)
        if not model:
            raise ValueError(f"Unknown model: {model_id}")

        client = await self._get_client(model.provider, model_id=model_id)
        # Google providers return ephemeral clients; close on exit so no
        # TLS connection outlives the stream.
        _owns_client = model.provider in (
            ModelProvider.GOOGLE,
            ModelProvider.GOOGLE_VERTEX,
        )
        body = self._build_request_body(
            model.provider,
            model_id,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=True,
            thinking_level=thinking_level,
            tool_config=tool_config,
            native_search_config=native_search_config,
            openai_local_runtime=openai_local_runtime,
        )

        try:
            if model.provider == ModelProvider.GOOGLE:
                endpoint = self._google_endpoint(model_id, stream=True)
                async for delta in self._stream_google(client, endpoint, body):
                    yield delta
            elif model.provider == ModelProvider.GOOGLE_VERTEX:
                endpoint = self._vertex_endpoint(model_id, stream=True)
                async for delta in self._stream_google(client, endpoint, body):
                    yield delta
            elif model.provider == ModelProvider.ANTHROPIC:
                endpoint = "/v1/messages"
                async for delta in self._stream_anthropic(client, endpoint, body):
                    yield delta
            elif self._uses_responses_v1(model.provider):
                endpoint = self._responses_endpoint(model.provider)
                async with _safe_provider_stream(client, endpoint, body) as response:
                    async for response_delta in iter_responses_stream(
                        response.aiter_lines(),
                        local_runtime=(
                            openai_local_runtime if model.provider == ModelProvider.OPENAI else None
                        ),
                    ):
                        yield StreamDelta(
                            content=response_delta.content,
                            tool_calls=response_delta.tool_calls,
                            finish_reason=response_delta.finish_reason,
                            usage=response_delta.usage,
                            thinking_content=response_delta.thinking_content,
                            provider_content_blocks=response_delta.provider_content_blocks,
                        )
            else:
                endpoint = "/v1/chat/completions"
                async for delta in self._stream_openai(client, endpoint, body):
                    yield delta
        finally:
            if _owns_client:
                await client.aclose()

    async def _stream_openai(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from OpenAI-compatible API."""
        # Stateful <think> tag parser for models that embed thinking in content
        in_think_block = False
        think_buf = ""
        saw_tool_call = False
        saw_terminal_event = False
        terminal_reason: str | None = None
        established_tool_calls: dict[int, tuple[str, str]] = {}

        async with _safe_provider_stream(client, endpoint, body) as response:
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    saw_terminal_event = True
                    break
                evt = _parse_sse_event(data_str, provider="openai-compatible")

                if "error" in evt:
                    error = evt.get("error")
                    error_type = error.get("type") if isinstance(error, dict) else None
                    if (
                        not isinstance(error_type, str)
                        or error_type not in _SAFE_OPENAI_ERROR_TYPES
                    ):
                        error_type = "provider_error"
                    raise ProviderStreamError("openai-compatible", error_type)

                # Handle usage - can appear in final chunk alongside choices
                usage_data = None
                if isinstance(evt.get("usage"), dict):
                    usage_data = _sanitize_usage(evt["usage"])
                    logger.debug(f"[USAGE] Received usage data: {usage_data}")

                # Safely get choices - may be empty list or missing
                choices = evt.get("choices", [])
                if not isinstance(choices, list):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                if saw_terminal_event:
                    if not choices and usage_data:
                        yield StreamDelta(usage=usage_data)
                        continue
                    raise ProviderStreamError("openai-compatible", "event_after_terminal")
                if not choices:
                    # No choices in this event, only yield if we have usage data
                    if usage_data:
                        yield StreamDelta(usage=usage_data)
                    continue

                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None and not isinstance(finish_reason, str):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                if finish_reason and finish_reason not in _SAFE_OPENAI_FINISH_REASONS:
                    raise ProviderStreamError("openai-compatible", "invalid_finish_reason")

                content = delta.get("content", "") or ""
                reasoning = delta.get("reasoning_content")
                if not isinstance(content, str) or (
                    reasoning is not None and not isinstance(reasoning, str)
                ):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                tool_calls = _validate_openai_tool_call_deltas(
                    delta.get("tool_calls"),
                    established_calls=established_tool_calls,
                )
                if tool_calls:
                    saw_tool_call = True
                if finish_reason:
                    saw_terminal_event = True
                    terminal_reason = finish_reason
                thinking = None

                # If provider gives reasoning_content natively, use it directly
                if reasoning:
                    thinking = reasoning
                elif content:
                    # Fallback: parse <think> tags from content stream
                    # Tags may be split across chunks, so track state
                    if in_think_block:
                        end_idx = content.find("</think>")
                        if end_idx != -1:
                            # Only yield the NEW portion from this chunk
                            thinking = content[:end_idx] if end_idx > 0 else None
                            think_buf = ""
                            in_think_block = False
                            content = content[end_idx + 8 :]  # skip </think>
                        else:
                            thinking = content
                            think_buf += content
                            content = ""
                    elif "<think>" in content:
                        start_idx = content.find("<think>")
                        pre_content = content[:start_idx]
                        rest = content[start_idx + 7 :]  # skip <think>
                        end_idx = rest.find("</think>")
                        if end_idx != -1:
                            thinking = rest[:end_idx]
                            content = pre_content + rest[end_idx + 8 :]
                        else:
                            thinking = rest
                            think_buf = rest
                            in_think_block = True
                            content = pre_content

                # Forward provider frames immediately.  A previous server-side
                # "typewriter" smoother split each frame into four-character
                # chunks and slept for 20 ms between them.  That blocked the
                # provider reader and made long answers tens of seconds slower.
                yield StreamDelta(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage=usage_data,
                    thinking_content=thinking,
                )
        if not saw_terminal_event:
            raise ProviderStreamError("openai-compatible", "incomplete_message")
        if saw_tool_call and terminal_reason not in {"tool_calls", "function_call"}:
            raise ProviderStreamError("openai-compatible", "incomplete_tool_call")

    async def _stream_anthropic(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Anthropic API."""
        tool_blocks: dict[int, dict[str, str]] = {}
        input_buffers: dict[int, str] = {}
        open_blocks: dict[int, str] = {}
        provider_blocks: dict[int, dict[str, Any]] = {}
        provider_block_order: list[int] = []
        saw_tool_call = False
        message_started = False
        message_stopped = False
        message_delta_started = False
        terminal_reason: str | None = None
        terminal_usage: dict[str, int] | None = None
        lifecycle_events = {
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        }

        def event_index(event: dict[str, Any]) -> int:
            raw_index = event.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderStreamError("anthropic", "invalid_event")
            return raw_index

        async with _safe_provider_stream(client, endpoint, body) as response:
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                evt = _parse_sse_event(data_str, provider="anthropic")
                evt_type = evt.get("type")
                if not isinstance(evt_type, str):
                    raise ProviderStreamError("anthropic", "invalid_event")

                if evt_type == "error":
                    error = evt.get("error")
                    error_type = (
                        str(error.get("type") or "provider_error")
                        if isinstance(error, dict)
                        else "provider_error"
                    )
                    if error_type not in _SAFE_ANTHROPIC_ERROR_TYPES:
                        error_type = "provider_error"
                    raise ProviderStreamError("anthropic", error_type)

                # Anthropic permits pings anywhere in the stream, including
                # between a terminal message_delta and message_stop.
                if evt_type == "ping":
                    continue

                # The versioning contract permits new event types. Ignore
                # unknown typed events while keeping known lifecycle events
                # strictly ordered and paired.
                if evt_type not in lifecycle_events:
                    continue

                if not message_started:
                    if evt_type != "message_start":
                        raise ProviderStreamError("anthropic", "invalid_event_order")
                    message_started = True
                    message = evt.get("message")
                    if not isinstance(message, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    raw_usage = message.get("usage", {})
                    if not isinstance(raw_usage, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    usage = _sanitize_usage(raw_usage)
                    if usage:
                        yield StreamDelta(usage=usage)
                    continue

                if message_delta_started:
                    if evt_type == "message_stop":
                        if open_blocks:
                            raise ProviderStreamError("anthropic", "incomplete_content_block")
                        if terminal_reason is None:
                            raise ProviderStreamError("anthropic", "incomplete_message")
                        message_stopped = True
                        yield StreamDelta(
                            finish_reason=terminal_reason,
                            usage=terminal_usage,
                            provider_content_blocks=[
                                copy.deepcopy(provider_blocks[index])
                                for index in provider_block_order
                            ],
                        )
                        break
                    if evt_type != "message_delta":
                        raise ProviderStreamError("anthropic", "event_after_terminal")

                if evt_type in {"message_start", "message_stop"}:
                    raise ProviderStreamError("anthropic", "invalid_event_order")

                if evt_type == "content_block_start":
                    index = event_index(evt)
                    if index in provider_blocks:
                        raise ProviderStreamError("anthropic", "invalid_event_order")
                    block = evt.get("content_block")
                    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    block_type = block["type"]
                    if not block_type:
                        raise ProviderStreamError("anthropic", "invalid_event")
                    open_blocks[index] = block_type
                    provider_blocks[index] = copy.deepcopy(block)
                    provider_block_order.append(index)
                    if block_type in {"tool_use", "server_tool_use"}:
                        tool_id = block.get("id")
                        tool_name = block.get("name")
                        if (
                            not isinstance(tool_id, str)
                            or not tool_id
                            or not isinstance(tool_name, str)
                            or not tool_name
                        ):
                            raise ProviderStreamError("anthropic", "invalid_tool_use")
                        initial_input = block.get("input")
                        initial_arguments = ""
                        if initial_input not in (None, {}):
                            if not isinstance(initial_input, dict):
                                raise ProviderStreamError("anthropic", "invalid_tool_input")
                            initial_arguments = json.dumps(
                                initial_input,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        input_buffers[index] = initial_arguments
                        if block_type == "server_tool_use":
                            continue
                        saw_tool_call = True
                        tool_blocks[index] = {
                            "id": tool_id,
                            "name": tool_name,
                            "arguments": initial_arguments,
                        }
                        yield StreamDelta(
                            tool_calls=[
                                {
                                    "index": index,
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": initial_arguments,
                                    },
                                }
                            ]
                        )
                    continue

                if evt_type == "content_block_delta":
                    index = event_index(evt)
                    block_type = open_blocks.get(index)
                    if block_type is None:
                        raise ProviderStreamError("anthropic", "orphan_content_block_delta")
                    delta = evt.get("delta")
                    if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    delta_type = delta["type"]
                    if block_type == "tool_use" and delta_type != "input_json_delta":
                        raise ProviderStreamError("anthropic", "invalid_tool_input")
                    if delta_type == "text_delta" and block_type != "text":
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type == "input_json_delta" and block_type not in {
                        "tool_use",
                        "server_tool_use",
                    }:
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type in {"thinking_delta", "signature_delta"} and block_type != (
                        "thinking"
                    ):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type == "citations_delta" and block_type != "text":
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if not isinstance(text, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block = provider_blocks[index]
                        existing_text = provider_block.get("text", "")
                        if not isinstance(existing_text, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block["text"] = existing_text + text
                        yield StreamDelta(content=text)
                    elif delta_type == "input_json_delta" and block_type in {
                        "tool_use",
                        "server_tool_use",
                    }:
                        partial_json = delta.get("partial_json", "")
                        if not isinstance(partial_json, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        input_buffers[index] = input_buffers.get(index, "") + partial_json
                        block = tool_blocks.get(index)
                        if block is not None:
                            block["arguments"] += partial_json
                        if partial_json and block_type == "tool_use":
                            yield StreamDelta(
                                tool_calls=[
                                    {
                                        "index": index,
                                        "function": {"arguments": partial_json},
                                    }
                                ]
                            )
                    elif block_type == "tool_use":
                        # Unknown deltas on a client-executable tool must fail
                        # closed. Ignoring them could turn malformed arguments
                        # into an executable empty object.
                        raise ProviderStreamError("anthropic", "invalid_tool_input")
                    elif delta_type == "thinking_delta":
                        thinking = delta.get("thinking", "")
                        if not isinstance(thinking, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block = provider_blocks[index]
                        existing = provider_block.get("thinking", "")
                        if not isinstance(existing, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block["thinking"] = existing + thinking
                        yield StreamDelta(thinking_content=thinking)
                    elif delta_type == "signature_delta":
                        signature = delta.get("signature")
                        if not isinstance(signature, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_blocks[index]["signature"] = signature
                    elif delta_type == "citations_delta":
                        citation = delta.get("citation")
                        if block_type != "text" or not isinstance(citation, dict):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        citations = provider_blocks[index].setdefault("citations", [])
                        if not isinstance(citations, list):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        citations.append(copy.deepcopy(citation))

                elif evt_type == "content_block_stop":
                    index = event_index(evt)
                    block_type = open_blocks.pop(index, None)
                    if block_type is None:
                        raise ProviderStreamError("anthropic", "invalid_event_order")
                    block = tool_blocks.pop(index, None)
                    if block_type in {"tool_use", "server_tool_use"}:
                        if block_type == "tool_use" and block is None:
                            raise ProviderStreamError("anthropic", "incomplete_tool_use")
                        invalid_arguments = False
                        try:
                            parsed_arguments = json.loads(input_buffers.pop(index, "") or "{}")
                        except json.JSONDecodeError:
                            invalid_arguments = True
                            parsed_arguments = None
                        if invalid_arguments:
                            raise ProviderStreamError("anthropic", "invalid_tool_input_json")
                        if not isinstance(parsed_arguments, dict):
                            raise ProviderStreamError("anthropic", "invalid_tool_input")
                        provider_blocks[index]["input"] = parsed_arguments

                elif evt_type == "message_delta":
                    if open_blocks:
                        raise ProviderStreamError("anthropic", "incomplete_content_block")
                    message_delta_started = True
                    delta = evt.get("delta")
                    if not isinstance(delta, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    stop_reason = delta.get("stop_reason")
                    if stop_reason is not None and not isinstance(stop_reason, str):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if stop_reason:
                        if stop_reason not in _SAFE_ANTHROPIC_STOP_REASONS:
                            raise ProviderStreamError("anthropic", "invalid_stop_reason")
                        if terminal_reason is not None and terminal_reason != stop_reason:
                            raise ProviderStreamError("anthropic", "invalid_event_order")
                        terminal_reason = stop_reason
                    raw_usage = evt.get("usage", {})
                    if not isinstance(raw_usage, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    usage = _sanitize_usage(raw_usage)
                    if usage:
                        terminal_usage = usage
                    yield StreamDelta(
                        finish_reason=stop_reason,
                        usage=usage or None,
                    )

            if tool_blocks:
                raise ProviderStreamError("anthropic", "incomplete_tool_use")
            if open_blocks:
                raise ProviderStreamError("anthropic", "incomplete_content_block")
            if not message_started or not message_stopped:
                raise ProviderStreamError("anthropic", "incomplete_message")
            if saw_tool_call and terminal_reason != "tool_use":
                raise ProviderStreamError("anthropic", "incomplete_tool_call")

    async def _stream_google(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Google Gemini API."""
        tool_count = sum(
            len(tool.get("functionDeclarations") or [])
            for tool in body.get("tools", [])
            if isinstance(tool, dict)
        )
        content_count = len(body.get("contents") or [])
        logger.info(
            "[GEMINI] request prepared: contents=%s tools=%s system=%s",
            content_count,
            tool_count,
            bool(body.get("systemInstruction")),
        )

        # Wire-level timing — helps diagnose whether a slow response is
        # client-side (context/tool-prep), network-side (httpx connect/TLS),
        # or server-side (model inference). Each phase is logged at INFO
        # so we don't need to flip debug levels in prod to debug latency.
        import time as _time

        _request_started = _time.perf_counter()
        _host = "unknown"
        safe_transport_error: httpx.RequestError | None = None
        try:
            from urllib.parse import urlparse as _urlparse

            _host = _urlparse(endpoint).hostname or "unknown"
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.models.model_registry.internal_failure", exc
            )
            pass
        logger.info(f"[GEMINI] HTTP POST → host={_host}")

        # Track functionCall parts already emitted in this stream to avoid
        # duplicate tool calls. Gemini streaming does not provide stable tool
        # call ids, and the same functionCall part can legitimately appear in
        # more than one SSE data frame (e.g. once in the content chunk, once
        # in the finish chunk). Since we synthesize a fresh uuid per part,
        # naive emission creates duplicate tool_call pills in the Activity
        # drawer. Key on (name, args_json) — thoughtSignature varies so we
        # don't include it in the dedup key.
        emitted_function_calls: set[tuple[str, str]] = set()

        try:
            stream_context = client.stream("POST", endpoint, json=body)
            async with stream_context as response:
                _headers_ms = (_time.perf_counter() - _request_started) * 1000
                logger.info(
                    f"[GEMINI] response headers received after {_headers_ms:.0f}ms "
                    f"(host={_host} status={response.status_code})"
                )
                if not 200 <= response.status_code < 300:
                    error_body = await response.aread()
                    request_id = (
                        response.headers.get("x-request-id")
                        or response.headers.get("x-goog-request-id")
                        or "unknown"
                    )
                    logger.error(
                        "[GEMINI] provider error: status=%s host=%s request_id=%s body_bytes=%s",
                        response.status_code,
                        _host,
                        request_id,
                        len(error_body),
                    )
                _raise_for_status_without_query_secrets(response)
                async for delta in self._consume_google_stream(
                    response,
                    request_started=_request_started,
                    host=_host,
                    emitted_function_calls=emitted_function_calls,
                ):
                    yield delta
        except httpx.HTTPStatusError:
            raise
        except httpx.RequestError as exc:
            safe_transport_error = _safe_request_error(exc)
        if safe_transport_error is not None:
            raise safe_transport_error

    async def _consume_google_stream(
        self,
        response: httpx.Response,
        *,
        request_started: float,
        host: str,
        emitted_function_calls: set[tuple[str, str]],
    ) -> AsyncIterator[StreamDelta]:
        """Consume a successful Gemini stream without retaining request secrets."""
        import time as _time

        _request_started = request_started
        _host = host
        _first_line_logged = False
        saw_tool_call = False
        saw_terminal_event = False
        terminal_reason: str | None = None
        async for line in response.aiter_lines():
            if line is not None:
                if not _first_line_logged:
                    _first_line_ms = (_time.perf_counter() - _request_started) * 1000
                    logger.info(
                        f"[GEMINI] first SSE line after {_first_line_ms:.0f}ms (host={_host})"
                    )
                    _first_line_logged = True
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                evt = _parse_sse_event(data_str, provider="google")
                candidates = evt.get("candidates", [])
                usage_meta = evt.get("usageMetadata", {})
                prompt_feedback = evt.get("promptFeedback")
                if not isinstance(candidates, list) or not isinstance(usage_meta, dict):
                    raise ProviderStreamError("google", "invalid_event")
                if prompt_feedback is not None and not isinstance(prompt_feedback, dict):
                    raise ProviderStreamError("google", "invalid_event")

                if saw_terminal_event:
                    if not candidates and usage_meta:
                        usage = _sanitize_usage(usage_meta)
                        if usage:
                            yield StreamDelta(usage=usage)
                        continue
                    raise ProviderStreamError("google", "event_after_terminal")

                # Check for promptFeedback (Safety blocking)
                if prompt_feedback and prompt_feedback.get("blockReason"):
                    raw_block_reason = str(prompt_feedback.get("blockReason") or "")
                    block_reason = (
                        raw_block_reason
                        if raw_block_reason in _SAFE_GOOGLE_BLOCK_REASONS
                        else "UNKNOWN"
                    )
                    logger.warning("[GEMINI] Response blocked: %s", block_reason)
                    saw_terminal_event = True
                    terminal_reason = "SAFETY"
                    yield StreamDelta(
                        finish_reason="safety",
                        content=f"\n\n[System: Response blocked due to safety reason: {block_reason}]",
                    )
                    continue

                if candidates:
                    candidate = candidates[0]
                    if not isinstance(candidate, dict):
                        raise ProviderStreamError("google", "invalid_event")
                    content = candidate.get("content", {})
                    if not isinstance(content, dict):
                        raise ProviderStreamError("google", "invalid_event")
                    parts = content.get("parts", [])
                    if not isinstance(parts, list):
                        raise ProviderStreamError("google", "invalid_event")

                    # Native search grounding — log chunks/citations at DEBUG.
                    # Frontend relies on inline markdown links for now; we
                    # don't emit a new SSE event in this first round.
                    grounding = candidate.get("groundingMetadata")
                    if grounding:
                        if not isinstance(grounding, dict):
                            raise ProviderStreamError("google", "invalid_event")
                        chunks = grounding.get("groundingChunks") or []
                        if not isinstance(chunks, list):
                            raise ProviderStreamError("google", "invalid_event")
                        logger.debug(f"[GEMINI] groundingChunks received: {len(chunks)} entries")

                    # Check for finishReason in candidate even if parts are empty
                    finish_reason = candidate.get("finishReason")
                    if finish_reason is not None and not isinstance(finish_reason, str):
                        raise ProviderStreamError("google", "invalid_event")
                    if finish_reason:
                        if finish_reason.upper() not in _SAFE_GOOGLE_FINISH_REASONS:
                            raise ProviderStreamError("google", "invalid_finish_reason")
                        saw_terminal_event = True
                        terminal_reason = finish_reason.upper()

                    if not parts and finish_reason:
                        # Handle case where only finishReason is sent (e.g. SAFETY, STOP)
                        yield StreamDelta(finish_reason=finish_reason.lower())

                    tool_calls_batch = []
                    for part in parts:
                        if not isinstance(part, dict):
                            raise ProviderStreamError("google", "invalid_event")
                        if part.get("thought") and "text" in part:
                            # Gemini 3 thinking content (thought parts).
                            # Guard against a Gemini quirk where thought text
                            # arrives with literal ``\n`` escape sequences
                            # instead of real newlines. Thought summaries are
                            # natural-language prose — legitimate occurrences
                            # of literal ``\n`` don't happen — so when we see
                            # ``\n`` strings AND no real newlines, unescape.
                            _thought_text = part["text"]
                            if not isinstance(_thought_text, str):
                                raise ProviderStreamError("google", "invalid_event")
                            if "\\n" in _thought_text and "\n" not in _thought_text:
                                with contextlib.suppress(
                                    UnicodeDecodeError,
                                    UnicodeEncodeError,
                                ):
                                    _thought_text = _thought_text.encode("utf-8").decode(
                                        "unicode_escape"
                                    )
                            yield StreamDelta(thinking_content=_thought_text)
                        elif "text" in part:
                            # SMOOTHER: Vertex Express Mode streams ~1 SSE frame
                            # per second with ~100 chars at a time (verified with
                            # direct curl against aiplatform.googleapis.com —
                            # list-30-facts yielded only 6 SSE events over 6.5s).
                            # Emitting that as a single StreamDelta makes the
                            # frontend render in 3-6 large bursts, which users
                            # perceive as "not streaming." Split each Vertex
                            # frame into smaller deltas with a small inter-chunk
                            # delay so the frontend sees token-like cadence.
                            _text = part["text"]
                            if not isinstance(_text, str):
                                raise ProviderStreamError("google", "invalid_event")
                            async for _sub in _smooth_text_delta(_text):
                                yield StreamDelta(content=_sub)
                        elif "functionCall" in part:
                            saw_tool_call = True
                            # Gemini 3 function call with optional thoughtSignature
                            fc = part["functionCall"]
                            # IMPORTANT: tool_call ids must be unique per call for the assistant UI.
                            # Gemini streaming does not provide a stable unique call id, so we generate one.
                            import uuid

                            if not isinstance(fc, dict):
                                raise ProviderStreamError("google", "invalid_event")
                            fc_name = fc.get("name") or "unknown"
                            fc_args = fc.get("args", {})
                            if not isinstance(fc_name, str) or not isinstance(fc_args, dict):
                                raise ProviderStreamError("google", "invalid_event")
                            fc_args_json = json.dumps(fc_args, sort_keys=True, ensure_ascii=False)
                            dedup_key = (str(fc_name), fc_args_json)
                            # args_hash helps diagnose duplicate-pill
                            # regressions (lets us see whether re-emitted
                            # chunks carry identical args or varied ones).
                            # Emit stays DEBUG to keep prod log volume sane;
                            # SKIP stays INFO — each skip is an actual signal.
                            _args_hash = hashlib.sha256(fc_args_json.encode("utf-8")).hexdigest()[
                                :10
                            ]
                            _name_hash = hashlib.sha256(str(fc_name).encode("utf-8")).hexdigest()[
                                :10
                            ]
                            if dedup_key in emitted_function_calls:
                                # Provider re-emitted the same functionCall in
                                # a later SSE chunk. Skip — downstream already
                                # accumulated it under a fresh uuid, and
                                # adding another copy would create a duplicate
                                # Activity-drawer pill.
                                logger.info(
                                    f"[GEMINI] functionCall SKIP (dup): "
                                    f"name_hash={_name_hash} args_hash={_args_hash}"
                                )
                                continue
                            emitted_function_calls.add(dedup_key)
                            logger.debug(
                                f"[GEMINI] functionCall emit: "
                                f"name_hash={_name_hash} args_hash={_args_hash}"
                            )

                            tool_call: dict[str, Any] = {
                                "id": f"call_{fc_name}_{uuid.uuid4().hex[:10]}",
                                "type": "function",
                                "function": {
                                    "name": fc_name,
                                    "arguments": fc_args_json,
                                },
                            }
                            # CRITICAL: Preserve thoughtSignature for Gemini 3
                            # This must be passed back in subsequent requests
                            if "thoughtSignature" in part:
                                tool_call["thoughtSignature"] = part["thoughtSignature"]
                                logger.debug("[GEMINI3] Captured thoughtSignature for tool call")
                            tool_calls_batch.append(tool_call)

                        # Capture standalone thoughtSignature if present (rare but possible)
                        elif "thoughtSignature" in part and "functionCall" not in part:
                            logger.debug("[GEMINI3] Captured standalone thoughtSignature")
                            ts = part["thoughtSignature"]
                            if not isinstance(ts, str):
                                raise ProviderStreamError("google", "invalid_event")
                            # If we have text content in the same part (which shouldn't happen based on API structure, but to be safe)
                            # Or if we want to yield it attached to text
                            yield StreamDelta(thought_signature=ts)

                    # Yield all tool calls together
                    if tool_calls_batch:
                        yield StreamDelta(tool_calls=tool_calls_batch)

                    # Check finish reason
                    if finish_reason:
                        yield StreamDelta(finish_reason=finish_reason.lower())

                else:
                    # No candidates - could be usage metadata only or keep-alive
                    pass

                # Handle usage metadata
                usage = _sanitize_usage(usage_meta) if usage_meta else {}
                if usage:
                    yield StreamDelta(usage=usage)
        if not saw_terminal_event:
            raise ProviderStreamError("google", "incomplete_message")
        if saw_tool_call and terminal_reason != "STOP":
            raise ProviderStreamError("google", "incomplete_tool_call")

    async def close(self) -> None:
        """Close all HTTP clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
