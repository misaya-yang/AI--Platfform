"""Strict OpenAI Responses-style ingress over the canonical Assistant AgentLoop.

This is a transport adapter, not a second model or tool loop.  Both streaming
and non-streaming requests consume ``AssistantService.chat_stream``.  The
initial contract is deliberately stateless: ``store=true``,
``previous_response_id`` and client-defined tools are rejected instead of
pretending that the server-executed platform tool model has OpenAI callback
semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import json
import math
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...auth import UserContext, get_user_context
from ...core.assistant_service import AssistantConfig, AssistantStreamEvent, RAGMode
from ...core.tool_invoker import CapabilityAllowlist
from ..deps import get_assistant_service, get_model_registry

router = APIRouter()

_ALLOWED_REQUEST_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "temperature",
        "max_output_tokens",
        "stream",
        "store",
        "previous_response_id",
        "tools",
    }
)
_MAX_INPUT_ITEMS = 200
_MAX_INPUT_CHARS = 200_000
# AgentLoop deliberately keeps client instructions in a lower-priority user
# context section and caps that section at 500 characters.  Reject above that
# exact ceiling here rather than silently truncating an API request.
_MAX_INSTRUCTIONS_CHARS = 500
_MAX_TOOL_ARGUMENT_CHARS = 100_000
_MAX_TOOL_OUTPUT_CHARS = 200_000


class ResponsesIngressError(ValueError):
    """Safe client-facing Responses request or projection failure."""

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        param: str | None = None,
        status_code: int = 400,
        error_type: str = "invalid_request_error",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or code.replace("_", " ")
        self.param = param
        self.status_code = status_code
        self.error_type = error_type


@dataclass(frozen=True)
class ParsedResponsesRequest:
    model_id: str
    message: str
    history: list[dict[str, Any]]
    instructions: str | None
    temperature: float
    max_output_tokens: int | None
    stream: bool
    store: bool
    config: AssistantConfig


def _require_string(
    value: Any,
    *,
    param: str,
    allow_empty: bool = False,
    max_chars: int,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ResponsesIngressError("invalid_string", param=param)
    if len(value) > max_chars or "\x00" in value:
        raise ResponsesIngressError("invalid_string", param=param)
    return value


def _parse_message_content(value: Any, *, role: str, param: str) -> str:
    if isinstance(value, str):
        return _require_string(
            value,
            param=param,
            max_chars=_MAX_INPUT_CHARS,
        )
    if not isinstance(value, list) or not value:
        raise ResponsesIngressError("invalid_message_content", param=param)
    parts: list[str] = []
    for index, part in enumerate(value):
        part_param = f"{param}[{index}]"
        if not isinstance(part, dict):
            raise ResponsesIngressError("invalid_message_content", param=part_param)
        unexpected = set(part) - {"type", "text", "annotations"}
        if unexpected:
            raise ResponsesIngressError(
                "unsupported_content_field",
                param=f"{part_param}.{sorted(unexpected)[0]}",
            )
        expected_type = "input_text" if role == "user" else "output_text"
        if part.get("type") not in {expected_type, "input_text"}:
            raise ResponsesIngressError("unsupported_content_type", param=f"{part_param}.type")
        annotations = part.get("annotations")
        if annotations not in (None, []):
            raise ResponsesIngressError(
                "unsupported_content_annotations",
                param=f"{part_param}.annotations",
            )
        parts.append(
            _require_string(
                part.get("text"),
                param=f"{part_param}.text",
                max_chars=_MAX_INPUT_CHARS,
            )
        )
    text = "".join(parts)
    if len(text) > _MAX_INPUT_CHARS:
        raise ResponsesIngressError("input_too_large", param=param)
    return text


def _parse_input(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(value, str):
        return (
            _require_string(value, param="input", max_chars=_MAX_INPUT_CHARS),
            [],
        )
    if not isinstance(value, list) or not value or len(value) > _MAX_INPUT_ITEMS:
        raise ResponsesIngressError("invalid_input", param="input")

    normalized: list[dict[str, Any]] = []
    pending_calls: dict[str, str] = {}
    seen_call_ids: set[str] = set()
    last_kind = ""
    total_chars = 0

    for index, item in enumerate(value):
        param = f"input[{index}]"
        if not isinstance(item, dict):
            raise ResponsesIngressError("invalid_input_item", param=param)
        item_type = item.get("type")
        if item_type in (None, "message"):
            unexpected = set(item) - {"type", "role", "content", "status", "id"}
            if unexpected:
                raise ResponsesIngressError(
                    "unsupported_input_item_field",
                    param=f"{param}.{sorted(unexpected)[0]}",
                )
            role = item.get("role")
            if role not in {"user", "assistant"}:
                raise ResponsesIngressError("unsupported_message_role", param=f"{param}.role")
            status = item.get("status")
            if status not in (None, "completed"):
                raise ResponsesIngressError("invalid_message_status", param=f"{param}.status")
            content = _parse_message_content(
                item.get("content"),
                role=str(role),
                param=f"{param}.content",
            )
            total_chars += len(content)
            normalized.append({"role": role, "content": content})
            last_kind = str(role)
            continue

        if item_type == "function_call":
            unexpected = set(item) - {
                "type",
                "id",
                "status",
                "call_id",
                "name",
                "arguments",
            }
            if unexpected:
                raise ResponsesIngressError(
                    "unsupported_input_item_field",
                    param=f"{param}.{sorted(unexpected)[0]}",
                )
            if item.get("status") not in (None, "completed"):
                raise ResponsesIngressError("invalid_function_call_status", param=f"{param}.status")
            call_id = _require_string(
                item.get("call_id"),
                param=f"{param}.call_id",
                max_chars=255,
            )
            name = _require_string(
                item.get("name"),
                param=f"{param}.name",
                max_chars=128,
            )
            arguments = _require_string(
                item.get("arguments"),
                param=f"{param}.arguments",
                max_chars=_MAX_TOOL_ARGUMENT_CHARS,
            )
            try:
                decoded_arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ResponsesIngressError(
                    "invalid_function_arguments", param=f"{param}.arguments"
                ) from exc
            if not isinstance(decoded_arguments, dict):
                raise ResponsesIngressError(
                    "invalid_function_arguments", param=f"{param}.arguments"
                )
            if call_id in seen_call_ids:
                raise ResponsesIngressError("duplicate_function_call", param=f"{param}.call_id")
            seen_call_ids.add(call_id)
            pending_calls[call_id] = name
            total_chars += len(arguments)
            normalized.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
            last_kind = "function_call"
            continue

        if item_type == "function_call_output":
            unexpected = set(item) - {"type", "id", "status", "call_id", "output"}
            if unexpected:
                raise ResponsesIngressError(
                    "unsupported_input_item_field",
                    param=f"{param}.{sorted(unexpected)[0]}",
                )
            if item.get("status") not in (None, "completed"):
                raise ResponsesIngressError(
                    "invalid_function_output_status", param=f"{param}.status"
                )
            call_id = _require_string(
                item.get("call_id"),
                param=f"{param}.call_id",
                max_chars=255,
            )
            name = pending_calls.pop(call_id, None)
            if name is None:
                raise ResponsesIngressError("orphan_function_call_output", param=f"{param}.call_id")
            output = _require_string(
                item.get("output"),
                param=f"{param}.output",
                allow_empty=True,
                max_chars=_MAX_TOOL_OUTPUT_CHARS,
            )
            total_chars += len(output)
            normalized.append(
                {
                    "role": "tool",
                    "content": output,
                    "tool_call_id": call_id,
                    "name": name,
                }
            )
            last_kind = "function_call_output"
            continue

        raise ResponsesIngressError("unsupported_input_item_type", param=f"{param}.type")

    if pending_calls:
        raise ResponsesIngressError("missing_function_call_output", param="input")
    if total_chars > _MAX_INPUT_CHARS:
        raise ResponsesIngressError("input_too_large", param="input")
    if last_kind == "user":
        current = normalized.pop()
        return str(current["content"]), normalized
    if last_kind == "function_call_output":
        return "Continue using the supplied function result.", normalized
    raise ResponsesIngressError("input_must_end_with_user_or_function_output", param="input")


def _validate_tools(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ResponsesIngressError("invalid_tools", param="tools")
    if not value:
        return
    first = value[0]
    if not isinstance(first, dict):
        raise ResponsesIngressError("invalid_tools", param="tools[0]")
    if first.get("type") != "function":
        raise ResponsesIngressError("built_in_tools_not_supported", param="tools")
    raise ResponsesIngressError("client_function_tools_not_supported", param="tools")


def parse_responses_request(payload: Any) -> ParsedResponsesRequest:
    """Parse the supported stateless Responses subset without coercion."""

    if not isinstance(payload, dict):
        raise ResponsesIngressError("invalid_json_object", param=None)
    unsupported = sorted(set(payload) - _ALLOWED_REQUEST_FIELDS)
    if unsupported:
        raise ResponsesIngressError("unsupported_field", param=unsupported[0])

    model_id = _require_string(payload.get("model"), param="model", max_chars=255)
    message, history = _parse_input(payload.get("input"))

    instructions_raw = payload.get("instructions")
    instructions = None
    if instructions_raw is not None:
        instructions = _require_string(
            instructions_raw,
            param="instructions",
            allow_empty=True,
            max_chars=_MAX_INSTRUCTIONS_CHARS,
        )

    temperature_raw = payload.get("temperature", 0.7)
    if (
        isinstance(temperature_raw, bool)
        or not isinstance(temperature_raw, (int, float))
        or not math.isfinite(float(temperature_raw))
        or not 0 <= float(temperature_raw) <= 2
    ):
        raise ResponsesIngressError("invalid_temperature", param="temperature")
    temperature = float(temperature_raw)

    max_output_tokens = payload.get("max_output_tokens")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise ResponsesIngressError("invalid_max_output_tokens", param="max_output_tokens")

    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise ResponsesIngressError("invalid_stream", param="stream")
    store = payload.get("store", False)
    if not isinstance(store, bool):
        raise ResponsesIngressError("invalid_store", param="store")
    if store:
        raise ResponsesIngressError("store_not_supported", param="store")
    if payload.get("previous_response_id") is not None:
        raise ResponsesIngressError(
            "previous_response_id_not_supported", param="previous_response_id"
        )
    _validate_tools(payload.get("tools"))

    config = AssistantConfig(
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_output_tokens,
        system_prompt=instructions,
        kb_mode=RAGMode.DISABLED,
        kb_dataset_ids=[],
        web_search_enabled=False,
        capability_allowlist=CapabilityAllowlist(),
        memory_mode="off",
        memory_profile="off",
        skills_enabled=False,
        runtime_mode="off",
        os_agent_enabled=False,
    )
    return ParsedResponsesRequest(
        model_id=model_id,
        message=message,
        history=history,
        instructions=instructions,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        stream=stream,
        store=False,
        config=config,
    )


def _usage(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        }
    if not isinstance(value, dict):
        raise ResponsesIngressError("invalid_usage", status_code=500)

    def token(name: str, *aliases: str) -> int:
        raw: Any = None
        for key in (name, *aliases):
            if key in value:
                raw = value[key]
                break
        if raw is None:
            return 0
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ResponsesIngressError("invalid_usage", status_code=500)
        return raw

    input_tokens = token("input_tokens", "prompt_tokens")
    output_tokens = token("output_tokens", "completion_tokens")
    details = value.get("input_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise ResponsesIngressError("invalid_usage", status_code=500)
    cached_tokens = token("cached_input_tokens")
    if cached_tokens == 0 and isinstance(details, dict) and "cached_tokens" in details:
        cached_raw = details["cached_tokens"]
        if isinstance(cached_raw, bool) or not isinstance(cached_raw, int) or cached_raw < 0:
            raise ResponsesIngressError("invalid_usage", status_code=500)
        cached_tokens = cached_raw
    if cached_tokens > input_tokens:
        raise ResponsesIngressError("invalid_usage", status_code=500)
    total_tokens = input_tokens + output_tokens
    supplied_total = value.get("total_tokens")
    if supplied_total is not None and (
        isinstance(supplied_total, bool)
        or not isinstance(supplied_total, int)
        or supplied_total != total_tokens
    ):
        raise ResponsesIngressError("invalid_usage", status_code=500)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": token("reasoning_tokens")},
        "total_tokens": total_tokens,
    }


class ResponsesStreamProjector:
    """Stateful, strictly sequenced projection of canonical Assistant events."""

    def __init__(
        self,
        *,
        response_id: str,
        session_id: str,
        model: str,
        instructions: str | None,
        temperature: float | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.response_id = response_id
        self.session_id = session_id
        self.model = model
        self.instructions = instructions
        self.temperature = temperature
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.created_at = int(time.time())
        self.sequence_number = 0
        self.run_id: str | None = None
        self.terminal = False
        self.output: list[dict[str, Any]] = []
        self.usage = _usage({})
        self._usage_seen = False
        self._transport_done: dict[str, Any] | None = None
        self._message_item: dict[str, Any] | None = None
        self._message_output_index: int | None = None
        self._text = ""
        self._run_started_seen = False

    def _event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "type": event_type,
            "sequence_number": self.sequence_number,
            **payload,
        }
        self.sequence_number += 1
        return event

    def _response(self, *, status: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = {"ai_gateway_session_id": self.session_id}
        if self.run_id:
            metadata["ai_gateway_run_id"] = self.run_id
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": error,
            "incomplete_details": None,
            "instructions": self.instructions,
            "model": self.model,
            "output": copy.deepcopy(self.output),
            "parallel_tool_calls": False,
            "previous_response_id": None,
            "store": False,
            "temperature": self.temperature,
            "tool_choice": "none",
            "tools": [],
            "usage": copy.deepcopy(self.usage) if status != "in_progress" else None,
            "metadata": metadata,
        }

    def created(self) -> list[dict[str, Any]]:
        return [self._event("response.created", response=self._response(status="in_progress"))]

    @staticmethod
    def _valid_run_id(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and value.strip() == value
            and value
            and len(value) <= 255
            and "\x00" not in value
        )

    def _bind_run(self, value: Any, *, required: bool = False) -> list[dict[str, Any]]:
        if value is None or value == "":
            return self.fail(code="missing_run_identity") if required else []
        if not self._valid_run_id(value):
            return self.fail(code="invalid_run_identity")
        candidate = value
        if self.run_id is not None and candidate != self.run_id:
            return self.fail(code="run_identity_mismatch")
        self.run_id = candidate
        return []

    @staticmethod
    def _event_type(event: AssistantStreamEvent) -> str:
        raw = event.event_type
        return str(getattr(raw, "value", raw) or "")

    @staticmethod
    def _data(event: AssistantStreamEvent) -> dict[str, Any]:
        return dict(event.data) if isinstance(event.data, dict) else {}

    def _start_message(self) -> list[dict[str, Any]]:
        if self._message_item is not None:
            return []
        output_index = len(self.output)
        item = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self.output.append(item)
        self._message_item = item
        self._message_output_index = output_index
        part = {"type": "output_text", "text": "", "annotations": []}
        return [
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=copy.deepcopy(item),
            ),
            self._event(
                "response.content_part.added",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                part=part,
            ),
        ]

    def _text_delta(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = value.get("delta", value.get("content", value.get("text")))
        if not isinstance(value, str) or not value:
            return self.fail(code="invalid_text_delta")
        events = self._start_message()
        if self.terminal:
            return events
        self._text += value
        assert self._message_item is not None
        assert self._message_output_index is not None
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self._message_item["id"],
                output_index=self._message_output_index,
                content_index=0,
                delta=value,
                logprobs=[],
            )
        )
        return events

    def _close_message(self) -> list[dict[str, Any]]:
        if self._message_item is None or self._message_item["status"] == "completed":
            return []
        assert self._message_output_index is not None
        part = {"type": "output_text", "text": self._text, "annotations": []}
        self._message_item["status"] = "completed"
        self._message_item["content"] = [part]
        return [
            self._event(
                "response.output_text.done",
                item_id=self._message_item["id"],
                output_index=self._message_output_index,
                content_index=0,
                text=self._text,
                logprobs=[],
            ),
            self._event(
                "response.content_part.done",
                item_id=self._message_item["id"],
                output_index=self._message_output_index,
                content_index=0,
                part=copy.deepcopy(part),
            ),
            self._event(
                "response.output_item.done",
                output_index=self._message_output_index,
                item=copy.deepcopy(self._message_item),
            ),
        ]

    def accept(self, event: AssistantStreamEvent) -> list[dict[str, Any]]:
        if self.terminal:
            raise ResponsesIngressError("event_after_terminal", status_code=500)
        event_type = self._event_type(event)
        data = self._data(event)
        is_tool_event = event_type.startswith("tool_")
        authoritative_terminals = {"run_finished", "run_error"}
        if self._transport_done is not None and event_type not in authoritative_terminals:
            return self.fail(code="event_after_transport_done")
        run_id_required = (
            event_type
            in {
                "run_started",
                "done",
                "run_finished",
                "run_error",
                "approval_required",
                "side_effect_unknown",
                "tool_call_start",
                "tool_call_end",
            }
            or is_tool_event
        )
        bound = self._bind_run(data.get("run_id"), required=run_id_required)
        if bound or self.terminal:
            return bound

        if event_type == "run_started":
            if self._run_started_seen:
                return self.fail(code="duplicate_run_started")
            self._run_started_seen = True
            return []
        if event_type == "text_delta":
            return self._text_delta(event.data)
        if event_type == "usage":
            try:
                self.usage = _usage(event.data)
            except ResponsesIngressError:
                return self.fail(code="invalid_usage")
            self._usage_seen = True
            return []
        if is_tool_event:
            return self.fail(code="unexpected_tool_event")
        if event_type == "run_error":
            return self._terminal_error(data)
        if event_type == "error":
            # Inner model/provider diagnostics are not authoritative.  The
            # canonical AgentLoop closes them with exactly one run_error.
            return []
        if event_type in {
            "approval_required",
            "side_effect_unknown",
        }:
            return self.fail(code=event_type)
        if event_type == "done":
            if self._transport_done is not None:
                return self.fail(code="duplicate_transport_done")
            self._transport_done = data
            return []
        if event_type == "run_finished":
            return self._terminal_success(data)
        return []

    def _validate_terminal_identity(
        self,
        data: dict[str, Any],
        *,
        expected_status: str | frozenset[str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        envelope = data.get("terminal_envelope")
        if not isinstance(envelope, dict):
            return None, "missing_terminal_envelope"
        if self.run_id is None:
            return None, "missing_run_identity"
        expected: dict[str, str | None] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "model_id": self.model,
        }
        for field, wanted in expected.items():
            if wanted is not None and str(envelope.get(field) or "") != wanted:
                return None, "terminal_identity_mismatch"
        allowed_statuses = (
            expected_status
            if isinstance(expected_status, frozenset)
            else frozenset({expected_status})
        )
        if envelope.get("status") not in allowed_statuses:
            return None, "terminal_identity_mismatch"
        return envelope, None

    def _terminal_error(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        envelope, identity_error = self._validate_terminal_identity(
            data,
            expected_status=frozenset({"failed", "cancelled"}),
        )
        if identity_error:
            return self.fail(code=identity_error)
        assert envelope is not None
        if not isinstance(envelope.get("usage"), dict):
            return self.fail(code="missing_terminal_usage")
        try:
            self.usage = _usage(envelope.get("usage"))
        except ResponsesIngressError:
            return self.fail(code="invalid_terminal_usage")
        code = str(envelope.get("exit_reason") or "server_error")
        return self.fail(code=code, preserve_usage=True)

    def _terminal_success(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        if self._transport_done is None:
            return self.fail(code="missing_transport_done")
        envelope, identity_error = self._validate_terminal_identity(
            data,
            expected_status="succeeded",
        )
        if identity_error:
            return self.fail(code=identity_error)
        assert envelope is not None
        if not isinstance(envelope.get("usage"), dict):
            return self.fail(code="missing_terminal_usage")
        try:
            terminal_usage = _usage(envelope.get("usage"))
        except ResponsesIngressError:
            return self.fail(code="invalid_terminal_usage")
        if self._usage_seen and terminal_usage != self.usage:
            return self.fail(code="terminal_usage_mismatch")
        self.usage = terminal_usage

        total_length = self._transport_done.get("total_length")
        if (
            isinstance(total_length, bool)
            or not isinstance(total_length, int)
            or total_length < 0
            or total_length != len(self._text)
        ):
            return self.fail(code="terminal_output_mismatch")
        return self.complete()

    def complete(self) -> list[dict[str, Any]]:
        if self.terminal:
            raise ResponsesIngressError("duplicate_terminal", status_code=500)
        if not self._text and not self.output:
            return self.fail(code="empty_response")
        events = self._close_message()
        self.terminal = True
        events.append(
            self._event("response.completed", response=self._response(status="completed"))
        )
        return events

    def fail(self, *, code: str, preserve_usage: bool = False) -> list[dict[str, Any]]:
        if self.terminal:
            raise ResponsesIngressError("duplicate_terminal", status_code=500)
        if not preserve_usage:
            self.usage = _usage({})
        self.terminal = True
        safe_error = {
            "code": code,
            "message": "The response could not be completed.",
            "type": "server_error",
        }
        return [
            self._event(
                "response.failed",
                response=self._response(status="failed", error=safe_error),
            )
        ]


def _sse(event: dict[str, Any]) -> str:
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def _close_async_iterator(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if callable(close):
        with contextlib.suppress(Exception):
            await close()


async def _iter_response_events(
    *,
    assistant: Any,
    parsed: ParsedResponsesRequest,
    user: UserContext,
    response_id: str,
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    projector = ResponsesStreamProjector(
        response_id=response_id,
        session_id=session_id,
        model=parsed.model_id,
        instructions=parsed.instructions,
        temperature=parsed.temperature,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    source = assistant.chat_stream(
        user=user,
        session_id=session_id,
        message=parsed.message,
        config=parsed.config,
        history=parsed.history,
        persist_messages=False,
    )
    first: AssistantStreamEvent | None = None
    startup_failed = False
    source_exhausted = False
    try:
        try:
            first = await anext(source)
        except StopAsyncIteration:
            source_exhausted = True
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:
            startup_failed = True

        if first is not None:
            first_run_id = projector._data(first).get("run_id")
            if projector._valid_run_id(first_run_id):
                projector.run_id = first_run_id

        for event in projector.created():
            yield event
        if startup_failed:
            for event in projector.fail(code="server_error"):
                yield event
            return

        async def canonical_events() -> AsyncIterator[AssistantStreamEvent]:
            if first is not None:
                yield first
            if not source_exhausted:
                async for assistant_event in source:
                    yield assistant_event

        pending_terminal: AssistantStreamEvent | None = None
        async for assistant_event in canonical_events():
            if pending_terminal is not None:
                for event in projector.fail(code="event_after_terminal"):
                    yield event
                break

            event_type = projector._event_type(assistant_event)
            if event_type in {"run_finished", "run_error"}:
                bind_events = projector._bind_run(
                    projector._data(assistant_event).get("run_id"),
                    required=True,
                )
                for event in bind_events:
                    yield event
                if projector.terminal:
                    break
                pending_terminal = assistant_event
                continue

            for event in projector.accept(assistant_event):
                yield event
            if projector.terminal:
                break
        else:
            if pending_terminal is not None:
                for event in projector.accept(pending_terminal):
                    yield event
            elif not projector.terminal:
                for event in projector.fail(code="incomplete_response"):
                    yield event
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception:
        if not projector.terminal:
            for event in projector.fail(code="server_error"):
                yield event
    finally:
        await _close_async_iterator(source)
        clear = getattr(assistant, "clear_session_runtime_state", None)
        if callable(clear):
            with contextlib.suppress(Exception):
                cleared = clear(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
                if inspect.isawaitable(cleared):
                    await cleared


async def iter_responses_sse(
    *,
    assistant: Any,
    parsed: ParsedResponsesRequest,
    user: UserContext,
    response_id: str,
    session_id: str,
) -> AsyncIterator[str]:
    """Yield strict Responses SSE frames and propagate disconnect cancellation."""

    async for event in _iter_response_events(
        assistant=assistant,
        parsed=parsed,
        user=user,
        response_id=response_id,
        session_id=session_id,
    ):
        yield _sse(event)


def _error_response(error: ResponsesIngressError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "message": error.message,
                "type": error.error_type,
                "param": error.param,
                "code": error.code,
            }
        },
    )


def _authenticated_identity(user: UserContext) -> bool:
    return bool(
        user.user_id
        and user.tenant_id
        and user.user_id != "anonymous"
        and user.tenant_id != "public"
    )


@router.post("/responses")
async def create_response(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Create one ephemeral response using the platform's canonical AgentLoop."""

    if getattr(request.state, "gateway_secret_verified", False) is not True:
        return _error_response(
            ResponsesIngressError(
                "gateway_authentication_required",
                message="A verified Gateway request is required.",
                status_code=401,
                error_type="authentication_error",
            )
        )
    if not _authenticated_identity(user):
        return _error_response(
            ResponsesIngressError(
                "authentication_required",
                message="Authentication and tenant identity are required.",
                status_code=401,
                error_type="authentication_error",
            )
        )
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _error_response(ResponsesIngressError("invalid_json"))
    try:
        parsed = parse_responses_request(payload)
    except ResponsesIngressError as exc:
        return _error_response(exc)

    model_registry = get_model_registry(request)
    get_model = getattr(model_registry, "get_model", None)
    configured = getattr(model_registry, "is_provider_configured", None)
    if not callable(get_model) or not callable(configured):
        return _error_response(
            ResponsesIngressError(
                "model_registry_unavailable",
                param="model",
                status_code=503,
                error_type="server_error",
            )
        )
    try:
        model_info = get_model(parsed.model_id)
    except Exception:
        return _error_response(
            ResponsesIngressError(
                "model_registry_unavailable",
                param="model",
                status_code=503,
                error_type="server_error",
            )
        )
    if model_info is None:
        return _error_response(
            ResponsesIngressError("model_not_found", param="model", status_code=400)
        )
    try:
        provider_configured = configured(model_info.provider)
    except Exception:
        provider_configured = False
    if not provider_configured:
        return _error_response(
            ResponsesIngressError(
                "model_not_available",
                param="model",
                status_code=503,
                error_type="server_error",
            )
        )
    model_output_limit = int(getattr(model_info, "max_output_tokens", 0) or 0)
    if (
        parsed.max_output_tokens is not None
        and model_output_limit > 0
        and parsed.max_output_tokens > model_output_limit
    ):
        return _error_response(
            ResponsesIngressError(
                "max_output_tokens_exceeds_model_limit",
                param="max_output_tokens",
                status_code=400,
            )
        )
    parsed.config.model_provider = model_info.provider
    parsed.config.traceparent = (
        getattr(request.state, "traceparent", None) or request.headers.get("traceparent") or None
    )

    assistant = get_assistant_service(request)
    response_id = f"resp_{uuid.uuid4().hex}"
    session_id = str(uuid.uuid4())
    if parsed.stream:
        return StreamingResponse(
            iter_responses_sse(
                assistant=assistant,
                parsed=parsed,
                user=user,
                response_id=response_id,
                session_id=session_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Response-Id": response_id,
            },
        )

    terminal: dict[str, Any] | None = None
    async for event in _iter_response_events(
        assistant=assistant,
        parsed=parsed,
        user=user,
        response_id=response_id,
        session_id=session_id,
    ):
        if event["type"] in {"response.completed", "response.failed"}:
            terminal = event
    if terminal is None:
        return _error_response(
            ResponsesIngressError("incomplete_response", status_code=500, error_type="server_error")
        )
    return JSONResponse(content=terminal["response"], headers={"X-Response-Id": response_id})


__all__ = [
    "ParsedResponsesRequest",
    "ResponsesIngressError",
    "ResponsesStreamProjector",
    "create_response",
    "iter_responses_sse",
    "parse_responses_request",
    "router",
]
