"""Strict OpenAI Responses-compatible upstream wire adapter.

This module translates the existing chat-message/tool contract into a
stateless ``POST /v1/responses`` request and reduces Responses SSE events into
the same deltas consumed by ``ModelRegistry``.  It is an upstream provider
adapter only; it does not expose a gateway ``/v1/responses`` route.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from ai_gateway_core.models import ChatMessage
from ai_gateway_core.models import normalize_chat_message as _normalize_message

CHAT_COMPLETIONS_WIRE_PROTOCOL = "chat_completions"
RESPONSES_V1_WIRE_PROTOCOL = "responses_v1"
SUPPORTED_WIRE_PROTOCOLS = frozenset({CHAT_COMPLETIONS_WIRE_PROTOCOL, RESPONSES_V1_WIRE_PROTOCOL})


class ResponsesAPIError(RuntimeError):
    """Prompt-safe Responses request or stream contract failure."""

    def __init__(self, error_type: str) -> None:
        self.provider = "openai-responses"
        self.error_type = error_type
        super().__init__(f"openai-responses failed ({error_type})")


@dataclass(frozen=True)
class ResponsesStreamDelta:
    """Provider-neutral delta projected into ``ModelRegistry.StreamDelta``."""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    thinking_content: str | None = None


@dataclass(frozen=True)
class ResponsesResult:
    """Validated non-streaming Responses result."""

    content: str
    usage: dict[str, int]
    tool_calls: list[dict[str, Any]]
    finish_reason: str


@dataclass
class _OutputBinding:
    output_index: int
    item_id: str
    item_type: str
    tool_index: int | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    arguments_done: bool = False
    text: str = ""
    text_done: bool = False
    item_done: bool = False
    server_tool_phase: int = -1
    server_tool_fingerprint: str | None = None


def _nonempty_string(value: Any, error_type: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponsesAPIError(error_type)
    return value


def _output_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResponsesAPIError("invalid_output_index")
    return value


def _validated_arguments(value: Any) -> str:
    if not isinstance(value, str):
        raise ResponsesAPIError("invalid_function_arguments")
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        raise ResponsesAPIError("invalid_function_arguments") from None
    if not isinstance(parsed, dict):
        raise ResponsesAPIError("invalid_function_arguments")
    return value


def _tool_call_from_chat(raw_call: Any) -> dict[str, Any]:
    if not isinstance(raw_call, dict) or raw_call.get("type", "function") != "function":
        raise ResponsesAPIError("invalid_function_call")
    call_id = _nonempty_string(raw_call.get("id"), "invalid_function_call")
    function = raw_call.get("function")
    if not isinstance(function, dict):
        raise ResponsesAPIError("invalid_function_call")
    name = _nonempty_string(function.get("name"), "invalid_function_call")
    arguments = _validated_arguments(function.get("arguments", ""))
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _responses_input(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

    items: list[dict[str, Any]] = []
    for raw_message in messages:
        message = _normalize_message(raw_message)
        role = str(message.role or "")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ResponsesAPIError("unsupported_message_role")
        if not isinstance(message.content, str):
            raise ResponsesAPIError("invalid_message_content")

        if role == "tool":
            if message.images or message.tool_calls:
                raise ResponsesAPIError("invalid_function_call_output")
            call_id = _nonempty_string(
                message.tool_call_id,
                "invalid_function_call_output",
            )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": message.content,
                }
            )
            continue

        if message.tool_call_id:
            raise ResponsesAPIError("invalid_message_tool_call_id")
        if message.tool_calls and role != "assistant":
            raise ResponsesAPIError("invalid_function_call")

        content = message.content
        if role == "system" and CACHE_SPLIT_MARKER in content:
            content = content.replace(CACHE_SPLIT_MARKER, "").replace("\n\n\n\n", "\n\n")

        if message.images:
            if role != "user":
                raise ResponsesAPIError("invalid_image_message")
            parts: list[dict[str, Any]] = [{"type": "input_text", "text": content}]
            for raw_image in message.images:
                if not isinstance(raw_image, str) or not raw_image:
                    raise ResponsesAPIError("invalid_image_message")
                image_url = (
                    raw_image
                    if raw_image.startswith(("http://", "https://", "data:"))
                    else f"data:image/jpeg;base64,{raw_image}"
                )
                parts.append({"type": "input_image", "image_url": image_url})
            items.append({"role": role, "content": parts})
        elif content or not message.tool_calls:
            items.append({"role": role, "content": content})

        for raw_call in message.tool_calls or []:
            items.append(_tool_call_from_chat(raw_call))

    if not items:
        raise ResponsesAPIError("empty_input")
    return items


def _responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_tool in tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            raise ResponsesAPIError("unsupported_tool_type")
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            raise ResponsesAPIError("invalid_tool_schema")
        name = _nonempty_string(function.get("name"), "invalid_tool_schema")
        if name in names:
            raise ResponsesAPIError("duplicate_tool_name")
        names.add(name)
        description = function.get("description", "")
        parameters = function.get("parameters")
        strict = function.get("strict")
        if not isinstance(description, str) or not isinstance(parameters, dict):
            raise ResponsesAPIError("invalid_tool_schema")
        if strict is not None and not isinstance(strict, bool):
            raise ResponsesAPIError("invalid_tool_schema")
        tool: dict[str, Any] = {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        if strict is not None:
            tool["strict"] = strict
        converted.append(tool)
    return converted


def build_responses_request(
    *,
    model_id: str,
    messages: list[ChatMessage],
    temperature: float,
    max_output_tokens: int | None,
    tools: list[dict[str, Any]] | None,
    stream: bool,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build a stateless Responses v1 request from the unified chat contract."""

    _nonempty_string(model_id, "invalid_model")
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise ResponsesAPIError("invalid_temperature")
    if not math.isfinite(float(temperature)):
        raise ResponsesAPIError("invalid_temperature")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise ResponsesAPIError("invalid_max_output_tokens")
    if reasoning_effort is not None and reasoning_effort not in {
        "minimal",
        "low",
        "medium",
        "high",
    }:
        raise ResponsesAPIError("invalid_reasoning_effort")

    body: dict[str, Any] = {
        "model": model_id,
        "input": _responses_input(messages),
        "temperature": float(temperature),
        "stream": bool(stream),
        "store": False,
    }
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    if tools:
        body["tools"] = _responses_tools(tools)
    if reasoning_effort is not None:
        body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
    return body


def _response_status(data: dict[str, Any]) -> str:
    status = data.get("status")
    if status == "failed" or data.get("error") is not None:
        raise ResponsesAPIError("response_failed")
    if status == "incomplete":
        raise ResponsesAPIError("response_incomplete")
    if status != "completed":
        raise ResponsesAPIError("invalid_response_status")
    return "completed"


def _responses_usage(value: Any) -> dict[str, int]:
    """Validate the Responses token accounting contract without coercion."""

    if not isinstance(value, dict):
        raise ResponsesAPIError("invalid_usage")

    tokens: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ResponsesAPIError("invalid_usage")
        tokens[key] = raw
    if tokens["total_tokens"] != tokens["input_tokens"] + tokens["output_tokens"]:
        raise ResponsesAPIError("invalid_usage")

    details = value.get("input_tokens_details")
    if details is not None:
        if not isinstance(details, dict):
            raise ResponsesAPIError("invalid_usage")
        if "cached_tokens" in details:
            cached = details["cached_tokens"]
            if (
                isinstance(cached, bool)
                or not isinstance(cached, int)
                or cached < 0
                or cached > tokens["input_tokens"]
            ):
                raise ResponsesAPIError("invalid_usage")
            tokens["cached_input_tokens"] = cached
    return tokens


def _message_output_text(item: dict[str, Any]) -> str:
    if item.get("role") not in (None, "assistant"):
        raise ResponsesAPIError("output_item_rebinding")
    blocks = item.get("content")
    if not isinstance(blocks, list):
        raise ResponsesAPIError("invalid_output_item")
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in {
            "output_text",
            "refusal",
        }:
            raise ResponsesAPIError("unsupported_output_content")
        text_key = "text" if block["type"] == "output_text" else "refusal"
        text = block.get(text_key)
        if not isinstance(text, str):
            raise ResponsesAPIError("invalid_output_content")
        parts.append(text)
    return "".join(parts)


def _function_call_from_output(item: dict[str, Any], index: int) -> dict[str, Any]:
    if item.get("status") not in (None, "completed"):
        raise ResponsesAPIError("incomplete_function_call")
    call_id = _nonempty_string(item.get("call_id"), "invalid_function_call")
    name = _nonempty_string(item.get("name"), "invalid_function_call")
    arguments = _validated_arguments(item.get("arguments", ""))
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _safe_server_tool_text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192 or "\x00" in value:
        raise ResponsesAPIError("invalid_server_tool_output")
    return value


def _web_search_fingerprint(item: dict[str, Any]) -> str:
    """Validate one completed server-side search item and retain no source text."""

    if item.get("status") != "completed":
        raise ResponsesAPIError("invalid_server_tool_lifecycle")
    action = item.get("action")
    if not isinstance(action, dict) or action.get("type") != "search":
        raise ResponsesAPIError("invalid_server_tool_output")

    normalized: dict[str, Any] = {"type": "search"}
    query = action.get("query")
    queries = action.get("queries")
    if query is None and queries is None:
        raise ResponsesAPIError("invalid_server_tool_output")
    if query is not None:
        normalized["query"] = _safe_server_tool_text(query)
    if queries is not None:
        if not isinstance(queries, list) or not queries or len(queries) > 100:
            raise ResponsesAPIError("invalid_server_tool_output")
        normalized["queries"] = [_safe_server_tool_text(value) for value in queries]

    sources = action.get("sources")
    if sources is not None:
        if not isinstance(sources, list) or len(sources) > 1000:
            raise ResponsesAPIError("invalid_server_tool_output")
        normalized_sources: list[dict[str, str]] = []
        for source in sources:
            if not isinstance(source, dict) or source.get("type") != "url":
                raise ResponsesAPIError("invalid_server_tool_output")
            source_url = _safe_server_tool_text(source.get("url"))
            try:
                parsed = urlsplit(source_url)
                valid_url = (
                    parsed.scheme in {"http", "https"}
                    and bool(parsed.hostname)
                    and parsed.username is None
                    and parsed.password is None
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ResponsesAPIError("invalid_server_tool_output")
            normalized_sources.append({"type": "url", "url": source_url})
        normalized["sources"] = normalized_sources

    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def parse_responses_response(data: Any) -> ResponsesResult:
    """Validate and parse one non-streaming Responses object."""

    if not isinstance(data, dict):
        raise ResponsesAPIError("invalid_response_json")
    if data.get("object") not in (None, "response"):
        raise ResponsesAPIError("invalid_response_object")
    _response_status(data)
    output = data.get("output")
    usage = data.get("usage")
    if not isinstance(output, list) or not isinstance(usage, dict):
        raise ResponsesAPIError("invalid_response")

    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            raise ResponsesAPIError("invalid_output_item")
        item_type = item.get("type")
        if item_type == "reasoning":
            continue
        if item_type == "web_search_call":
            _web_search_fingerprint(item)
            continue
        if item_type == "function_call":
            tool_calls.append(_function_call_from_output(item, len(tool_calls)))
            continue
        if item_type != "message" or item.get("status") not in (None, "completed"):
            raise ResponsesAPIError("unsupported_output_item")
        content_parts.append(_message_output_text(item))

    content = "".join(content_parts)
    if not content and not tool_calls:
        raise ResponsesAPIError("empty_response_output")
    return ResponsesResult(
        content=content,
        usage=_responses_usage(usage),
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
    )


def _event_binding(
    event: dict[str, Any],
    bindings: dict[int, _OutputBinding],
    *,
    expected_type: str,
) -> _OutputBinding:
    index = _output_index(event.get("output_index"))
    binding = bindings.get(index)
    if binding is None:
        raise ResponsesAPIError("orphan_output_event")
    item_id = _nonempty_string(event.get("item_id"), "invalid_output_item")
    if binding.item_id != item_id or binding.item_type != expected_type:
        raise ResponsesAPIError("output_item_rebinding")
    if binding.item_done:
        raise ResponsesAPIError("event_after_output_item_done")
    return binding


def _validate_response_identity(response: Any, response_id: str | None) -> str:
    if not isinstance(response, dict):
        raise ResponsesAPIError("invalid_response")
    current_id = _nonempty_string(response.get("id"), "invalid_response")
    if response_id is not None and current_id != response_id:
        raise ResponsesAPIError("response_rebinding")
    return current_id


def _sequence_number(event: dict[str, Any], previous: int | None) -> int:
    value = event.get("sequence_number")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResponsesAPIError("invalid_sequence_number")
    if (previous is None and value != 0) or (previous is not None and value != previous + 1):
        raise ResponsesAPIError("invalid_event_sequence")
    return value


def _validate_done_item_identity(item: dict[str, Any], binding: _OutputBinding) -> None:
    if item.get("id") != binding.item_id or item.get("type") != binding.item_type:
        raise ResponsesAPIError("output_item_rebinding")
    if item.get("status") not in (None, "completed"):
        raise ResponsesAPIError("incomplete_output_item")


def _reconcile_message_item(item: dict[str, Any], binding: _OutputBinding) -> str:
    final_text = _message_output_text(item)
    if binding.text_done:
        if final_text != binding.text:
            raise ResponsesAPIError("text_rebinding")
        return ""
    if not final_text.startswith(binding.text):
        raise ResponsesAPIError("text_rebinding")
    suffix = final_text[len(binding.text) :]
    binding.text = final_text
    binding.text_done = True
    return suffix


def _validate_function_item(item: dict[str, Any], binding: _OutputBinding) -> None:
    if not binding.arguments_done:
        raise ResponsesAPIError("incomplete_function_call")
    if (
        item.get("call_id") != binding.call_id
        or item.get("name") != binding.name
        or item.get("arguments") != binding.arguments
    ):
        raise ResponsesAPIError("function_call_rebinding")
    _validated_arguments(item.get("arguments"))


def _validate_web_search_item(item: dict[str, Any], binding: _OutputBinding) -> None:
    if binding.server_tool_phase != 2:
        raise ResponsesAPIError("invalid_server_tool_lifecycle")
    fingerprint = _web_search_fingerprint(item)
    if binding.server_tool_fingerprint is None:
        binding.server_tool_fingerprint = fingerprint
    elif binding.server_tool_fingerprint != fingerprint:
        raise ResponsesAPIError("server_tool_rebinding")


def _validate_completed_output(
    response: dict[str, Any],
    bindings: dict[int, _OutputBinding],
) -> None:
    output = response.get("output")
    if not isinstance(output, list):
        raise ResponsesAPIError("invalid_completed_output")
    if len(output) != len(bindings):
        raise ResponsesAPIError("output_item_rebinding")
    for index, item in enumerate(output):
        binding = bindings.get(index)
        if binding is None or not isinstance(item, dict):
            raise ResponsesAPIError("output_item_rebinding")
        _validate_done_item_identity(item, binding)
        if binding.item_type == "message":
            if _message_output_text(item) != binding.text:
                raise ResponsesAPIError("text_rebinding")
        elif binding.item_type == "function_call":
            _validate_function_item(item, binding)
        elif binding.item_type == "web_search_call":
            _validate_web_search_item(item, binding)


async def iter_responses_stream(
    lines: AsyncIterable[str],
) -> AsyncIterator[ResponsesStreamDelta]:
    """Reduce Responses SSE lines with strict item and terminal lifecycle checks."""

    response_id: str | None = None
    bindings: dict[int, _OutputBinding] = {}
    item_ids: set[str] = set()
    call_ids: set[str] = set()
    next_tool_index = 0
    terminal = False
    transport_done = False
    last_sequence: int | None = None

    async for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            if transport_done:
                raise ResponsesAPIError("duplicate_done")
            if not terminal:
                raise ResponsesAPIError("incomplete_response")
            transport_done = True
            continue
        if terminal or transport_done:
            raise ResponsesAPIError("event_after_terminal")
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            raise ResponsesAPIError("invalid_sse_json") from None
        if not isinstance(event, dict):
            raise ResponsesAPIError("invalid_event")
        last_sequence = _sequence_number(event, last_sequence)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ResponsesAPIError("invalid_event")

        if event_type == "error":
            raise ResponsesAPIError("provider_error")
        if event_type == "response.failed":
            raise ResponsesAPIError("response_failed")
        if event_type == "response.incomplete":
            raise ResponsesAPIError("response_incomplete")
        if event_type == "response.created":
            if response_id is not None:
                raise ResponsesAPIError("response_rebinding")
            response_id = _validate_response_identity(event.get("response"), None)
            continue
        if response_id is None:
            raise ResponsesAPIError("orphan_response_event")
        if event_type == "response.in_progress":
            _validate_response_identity(event.get("response"), response_id)
            continue

        if event_type == "response.output_item.added":
            index = _output_index(event.get("output_index"))
            item = event.get("item")
            if not isinstance(item, dict):
                raise ResponsesAPIError("invalid_output_item")
            item_id = _nonempty_string(item.get("id"), "invalid_output_item")
            item_type = item.get("type")
            if item_type not in {
                "message",
                "reasoning",
                "function_call",
                "web_search_call",
            }:
                raise ResponsesAPIError("unsupported_output_item")
            if index in bindings or item_id in item_ids:
                raise ResponsesAPIError("output_item_rebinding")
            if item_type == "web_search_call" and item.get("status") != "in_progress":
                raise ResponsesAPIError("invalid_server_tool_lifecycle")
            item_ids.add(item_id)
            binding = _OutputBinding(index, item_id, item_type)
            if item_type == "function_call":
                binding.call_id = _nonempty_string(
                    item.get("call_id"),
                    "invalid_function_call",
                )
                binding.name = _nonempty_string(item.get("name"), "invalid_function_call")
                if binding.call_id in call_ids:
                    raise ResponsesAPIError("function_call_rebinding")
                call_ids.add(binding.call_id)
                initial_arguments = item.get("arguments", "")
                if not isinstance(initial_arguments, str):
                    raise ResponsesAPIError("invalid_function_arguments")
                binding.arguments = initial_arguments
                binding.tool_index = next_tool_index
                next_tool_index += 1
                bindings[index] = binding
                yield ResponsesStreamDelta(
                    tool_calls=[
                        {
                            "index": binding.tool_index,
                            "id": binding.call_id,
                            "type": "function",
                            "function": {
                                "name": binding.name,
                                "arguments": initial_arguments,
                            },
                        }
                    ]
                )
                continue
            bindings[index] = binding
            continue

        if event_type in {
            "response.web_search_call.in_progress",
            "response.web_search_call.searching",
            "response.web_search_call.completed",
        }:
            binding = _event_binding(event, bindings, expected_type="web_search_call")
            phase = {
                "response.web_search_call.in_progress": 0,
                "response.web_search_call.searching": 1,
                "response.web_search_call.completed": 2,
            }[event_type]
            if phase != binding.server_tool_phase + 1:
                raise ResponsesAPIError("invalid_server_tool_lifecycle")
            binding.server_tool_phase = phase
            continue

        if event_type in {
            "response.content_part.added",
            "response.content_part.done",
        }:
            _event_binding(event, bindings, expected_type="message")
            continue
        if event_type in {
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
        }:
            _event_binding(event, bindings, expected_type="reasoning")
            continue

        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            binding = _event_binding(event, bindings, expected_type="message")
            if binding.text_done:
                raise ResponsesAPIError("event_after_text_done")
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ResponsesAPIError("invalid_text_delta")
            binding.text += delta
            if delta:
                yield ResponsesStreamDelta(content=delta)
            continue
        if event_type in {"response.output_text.done", "response.refusal.done"}:
            binding = _event_binding(event, bindings, expected_type="message")
            if binding.text_done:
                raise ResponsesAPIError("text_rebinding")
            final_key = "text" if event_type == "response.output_text.done" else "refusal"
            final_text = event.get(final_key)
            if not isinstance(final_text, str) or not final_text.startswith(binding.text):
                raise ResponsesAPIError("text_rebinding")
            suffix = final_text[len(binding.text) :]
            binding.text = final_text
            binding.text_done = True
            if suffix:
                yield ResponsesStreamDelta(content=suffix)
            continue

        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            binding = _event_binding(event, bindings, expected_type="reasoning")
            if binding.text_done:
                raise ResponsesAPIError("event_after_reasoning_done")
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ResponsesAPIError("invalid_reasoning_delta")
            binding.text += delta
            if delta:
                yield ResponsesStreamDelta(thinking_content=delta)
            continue
        if event_type in {
            "response.reasoning_summary_text.done",
            "response.reasoning_text.done",
        }:
            binding = _event_binding(event, bindings, expected_type="reasoning")
            if binding.text_done:
                raise ResponsesAPIError("reasoning_rebinding")
            final_text = event.get("text")
            if not isinstance(final_text, str) or not final_text.startswith(binding.text):
                raise ResponsesAPIError("reasoning_rebinding")
            suffix = final_text[len(binding.text) :]
            binding.text = final_text
            binding.text_done = True
            if suffix:
                yield ResponsesStreamDelta(thinking_content=suffix)
            continue

        if event_type == "response.function_call_arguments.delta":
            binding = _event_binding(event, bindings, expected_type="function_call")
            if binding.arguments_done:
                raise ResponsesAPIError("event_after_function_arguments_done")
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ResponsesAPIError("invalid_function_arguments")
            binding.arguments += delta
            if delta:
                yield ResponsesStreamDelta(
                    tool_calls=[
                        {
                            "index": binding.tool_index,
                            "function": {"arguments": delta},
                        }
                    ]
                )
            continue
        if event_type == "response.function_call_arguments.done":
            binding = _event_binding(event, bindings, expected_type="function_call")
            if binding.arguments_done:
                raise ResponsesAPIError("function_arguments_rebinding")
            arguments = event.get("arguments")
            if not isinstance(arguments, str) or not arguments.startswith(binding.arguments):
                raise ResponsesAPIError("function_arguments_rebinding")
            suffix = arguments[len(binding.arguments) :]
            _validated_arguments(arguments)
            binding.arguments = arguments
            binding.arguments_done = True
            if suffix:
                yield ResponsesStreamDelta(
                    tool_calls=[
                        {
                            "index": binding.tool_index,
                            "function": {"arguments": suffix},
                        }
                    ]
                )
            continue

        if event_type == "response.output_item.done":
            index = _output_index(event.get("output_index"))
            done_binding = bindings.get(index)
            item = event.get("item")
            if done_binding is None or not isinstance(item, dict) or done_binding.item_done:
                raise ResponsesAPIError("orphan_output_event")
            if item.get("id") != done_binding.item_id or item.get("type") != done_binding.item_type:
                raise ResponsesAPIError("output_item_rebinding")
            _validate_done_item_identity(item, done_binding)
            if done_binding.item_type == "function_call":
                _validate_function_item(item, done_binding)
            elif done_binding.item_type == "message":
                suffix = _reconcile_message_item(item, done_binding)
                if suffix:
                    yield ResponsesStreamDelta(content=suffix)
            elif done_binding.item_type == "web_search_call":
                _validate_web_search_item(item, done_binding)
            done_binding.item_done = True
            continue

        if event_type == "response.completed":
            response = event.get("response")
            _validate_response_identity(response, response_id)
            assert isinstance(response, dict)
            _response_status(response)
            if any(not binding.item_done for binding in bindings.values()):
                raise ResponsesAPIError("incomplete_output_item")
            if any(
                binding.item_type == "function_call" and not binding.arguments_done
                for binding in bindings.values()
            ):
                raise ResponsesAPIError("incomplete_function_call")
            has_tools = any(binding.item_type == "function_call" for binding in bindings.values())
            has_message_text = any(
                binding.item_type == "message" and bool(binding.text)
                for binding in bindings.values()
            )
            if not has_tools and not has_message_text:
                raise ResponsesAPIError("empty_response_output")
            _validate_completed_output(response, bindings)
            usage = _responses_usage(response.get("usage"))
            terminal = True
            yield ResponsesStreamDelta(
                finish_reason="tool_calls" if has_tools else "stop",
                usage=usage,
            )
            continue

        raise ResponsesAPIError("unsupported_event")

    if not terminal:
        raise ResponsesAPIError("incomplete_response")
