"""Strictly private, lease-bound model-only data plane for Agent.

This service never invokes an Agent loop. It validates one immutable runtime
snapshot, reserves one idempotent model-call budget, performs exactly one
provider HTTP request, and projects the provider stream back to the Responses
protocol consumed by the Agent kernel.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from ai_gateway_core.agents import (
    RuntimeModelLeaseClaims,
    RuntimeModelLeaseError,
    RuntimeModelLeaseSigner,
    canonical_runtime_json,
)
from ai_gateway_core.models import ReasoningWireError, apply_reasoning_wire


class _Database(Protocol):
    async def fetchrow(self, query: str, *args): ...

    async def execute(self, query: str, *args): ...


class AgentModelPlaneError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _runtime_snapshot(value: Any) -> dict[str, Any]:
    """Normalize asyncpg JSONB codecs without weakening snapshot validation."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, dict):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    return value


def _snapshot_parameters(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return provider parameters pinned by the immutable control snapshot."""

    raw = snapshot.get("parameters")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    temperature = raw.get("temperature")
    if temperature is None:
        return {}
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(float(temperature))
        or not 0 <= float(temperature) <= 2
    ):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    return {"temperature": temperature}


@dataclass(frozen=True, slots=True)
class _AuthorizedCall:
    call_id: uuid.UUID
    lease_id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: str
    user_id: str
    session_id: str
    model_id: str
    provider_id: str
    provider_revision: str
    snapshot: dict[str, Any]
    estimated_input_tokens: int
    reserved_output_tokens: int


def _timestamp_ms(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_TIME_INVALID", status_code=503)
    return int(value.timestamp() * 1000)


def _provider_revision(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _runtime_scope_sha256(tenant_id: str, user_id: str, session_id: str) -> str:
    digest = sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _estimate_tokens(value: Any) -> int:
    encoded = canonical_runtime_json(value)
    return max(1, math.ceil(len(encoded.encode("utf-8")) / 4))


def _cost_microusd(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> int:
    return max(
        0,
        math.ceil(
            input_tokens * max(input_price_per_1k, 0.0) * 1_000
            + output_tokens * max(output_price_per_1k, 0.0) * 1_000
        ),
    )


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentModelPlaneError("RUNTIME_PROVIDER_ENDPOINT_INVALID", status_code=503)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return urlunsplit(parsed)
    return urlunsplit(parsed._replace(path=f"{path}/chat/completions"))


def _responses_url(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentModelPlaneError("RUNTIME_PROVIDER_ENDPOINT_INVALID", status_code=503)
    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        return urlunsplit(parsed)
    if path.endswith("/v1"):
        return urlunsplit(parsed._replace(path=f"{path}/responses"))
    return urlunsplit(parsed._replace(path=f"{path}/v1/responses"))


def _validate_phase2_responses_input(
    body: Mapping[str, Any], *, allow_tool_transcript: bool = False
) -> None:
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        if not raw_input:
            raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
        return
    if not isinstance(raw_input, list) or not raw_input:
        raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
    for item in raw_input:
        if not isinstance(item, Mapping):
            raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
        item_type = item.get("type")
        if item_type in {"function_call", "function_call_output"} and not allow_tool_transcript:
            raise AgentModelPlaneError("RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE", status_code=422)


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class _ValidatedNativeTools:
    tools: list[dict[str, Any]]
    aliases: dict[str, tuple[str, str]]


def _function_tool(
    raw: Mapping[str, Any],
    *,
    wire_name: str | None = None,
    description_prefix: str = "",
) -> dict[str, Any]:
    name = raw.get("name")
    description = raw.get("description")
    parameters = raw.get("parameters")
    if (
        not isinstance(name, str)
        or not _TOOL_NAME_RE.fullmatch(name)
        or not isinstance(description, str)
        or len(description) > 16_384
        or not isinstance(parameters, Mapping)
    ):
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
    tool = {
        "type": "function",
        "name": wire_name or name,
        "description": f"{description_prefix}{description}",
        "parameters": copy.deepcopy(dict(parameters)),
    }
    if isinstance(raw.get("strict"), bool):
        tool["strict"] = raw["strict"]
    return tool


def _namespace_alias(namespace: str, name: str) -> str:
    digest = sha256(f"{namespace}\0{name}".encode()).hexdigest()[:10]
    prefix = f"ns_{digest}_"
    return f"{prefix}{name[: 64 - len(prefix)]}"


def _validated_native_tools(value: Any, profile: Mapping[str, Any]) -> _ValidatedNativeTools:
    if value in (None, []):
        return _ValidatedNativeTools(tools=[], aliases={})
    if not isinstance(value, list) or len(value) > 256:
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
    tool_capabilities = profile.get("tools")
    if not isinstance(tool_capabilities, Mapping):
        raise AgentModelPlaneError("RUNTIME_TOOL_CAPABILITY_INVALID", status_code=422)
    namespace_wire = tool_capabilities.get("namespace_wire")
    web_search_wire = tool_capabilities.get("web_search_wire")
    tools: list[dict[str, Any]] = []
    wire_names: set[str] = set()
    aliases: dict[str, tuple[str, str]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
        tool_type = raw.get("type")
        if tool_type == "web_search":
            if web_search_wire != "native":
                raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
            tools.append({"type": "web_search"})
            continue
        if tool_type == "namespace":
            if namespace_wire != "flatten":
                raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
            namespace = raw.get("name")
            children = raw.get("tools")
            if (
                not isinstance(namespace, str)
                or not _TOOL_NAME_RE.fullmatch(namespace)
                or not isinstance(children, list)
                or len(children) > 256
            ):
                raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
            for child in children:
                if not isinstance(child, Mapping) or child.get("type") != "function":
                    raise AgentModelPlaneError(
                        "RUNTIME_TOOL_SCHEMA_UNSUPPORTED",
                        status_code=422,
                    )
                child_name = child.get("name")
                if not isinstance(child_name, str):
                    raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
                alias = _namespace_alias(namespace, child_name)
                if alias in wire_names:
                    raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
                wire_names.add(alias)
                aliases[alias] = (namespace, child_name)
                tools.append(
                    _function_tool(
                        child,
                        wire_name=alias,
                        description_prefix=f"[{namespace}] ",
                    )
                )
            continue
        if tool_type != "function":
            raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
        name = raw.get("name")
        if not isinstance(name, str) or name in wire_names:
            raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
        wire_names.add(name)
        tools.append(_function_tool(raw))
    if len(tools) > 256:
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
    return _ValidatedNativeTools(tools=tools, aliases=aliases)


def _native_responses_body(
    body: Mapping[str, Any],
    *,
    model_id: str,
    max_output_tokens: int,
    profile: Mapping[str, Any],
    reasoning_option: str,
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    validated_tools = _validated_native_tools(body.get("tools"), profile)
    allow_function_transcript = any(
        tool.get("type") == "function" for tool in validated_tools.tools
    )
    _validate_phase2_responses_input(
        body,
        allow_tool_transcript=allow_function_transcript,
    )
    raw_input = body.get("input")
    if isinstance(raw_input, list) and any(
        isinstance(item, Mapping)
        and item.get("type") in {"function_call", "function_call_output"}
        for item in raw_input
    ):
        _validate_tool_transcript(
            raw_input,
            allowed_tool_names={
                str(tool["name"])
                for tool in validated_tools.tools
                if tool.get("type") == "function"
            },
        )
    result: dict[str, Any] = {
        "model": model_id,
        "input": copy.deepcopy(body["input"]),
        "stream": True,
        "store": False,
        "max_output_tokens": max_output_tokens,
    }
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        result["instructions"] = instructions
    if validated_tools.tools:
        result["tools"] = validated_tools.tools
        result["tool_choice"] = "auto"
    for key in ("temperature", "top_p"):
        value = body.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = value
    apply_reasoning_wire(result, profile, reasoning_option)
    return result, validated_tools.aliases


def _provider_headers(profile: Mapping[str, Any], api_key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    prompt_cache = profile.get("prompt_cache")
    if isinstance(prompt_cache, Mapping):
        adapter_id = prompt_cache.get("adapter_id")
        config = prompt_cache.get("config")
        if (
            adapter_id == "cache/dashscope-session-v1"
            and isinstance(config, Mapping)
            and config.get("enabled") is True
        ):
            headers["x-dashscope-session-cache"] = "enable"
    return headers


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for part in value:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _validate_tool_transcript(
    raw_input: list[Any], *, allowed_tool_names: set[str] | None = None
) -> None:
    pending_calls: dict[str, str] = {}
    completed_calls: set[str] = set()
    for item in raw_input:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not _TOOL_NAME_RE.fullmatch(name)
                or not isinstance(arguments, str)
                or call_id in pending_calls
                or call_id in completed_calls
                or (allowed_tool_names is not None and name not in allowed_tool_names)
            ):
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID", status_code=422)
            pending_calls[call_id] = name
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if (
                not isinstance(call_id, str)
                or call_id not in pending_calls
                or not isinstance(item.get("output"), str)
            ):
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID", status_code=422)
            pending_calls.pop(call_id)
            completed_calls.add(call_id)
    if pending_calls:
        raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID", status_code=422)


def _responses_input_to_messages(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
        return messages
    if not isinstance(raw_input, list):
        raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")

    _validate_tool_transcript(raw_input)

    pending_calls: dict[str, str] = {}
    for item in raw_input:
        if not isinstance(item, Mapping):
            raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
        item_type = item.get("type")
        if item_type in {None, "message"}:
            role = item.get("role")
            if role not in {"user", "assistant", "developer", "system"}:
                continue
            text = _content_text(item.get("content"))
            if text:
                messages.append(
                    {"role": "system" if role == "developer" else role, "content": text}
                )
            continue
        # Reasoning items are provider-owned opaque state. They are never
        # converted into model-visible text on a compatibility wire.
        if item_type == "reasoning":
            continue
        if item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            arguments = str(item.get("arguments") or "")
            if not call_id or not name:
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
            if call_id in pending_calls:
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
            pending_calls[call_id] = name
            messages.append(
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
            continue
        if item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if call_id not in pending_calls:
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": pending_calls.pop(call_id),
                    "content": str(item.get("output") or ""),
                }
            )
            continue
    if pending_calls:
        raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
    if not messages:
        raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
    return messages


class _ResponsesProjector:
    def __init__(self, *, model_id: str, estimated_input_tokens: int) -> None:
        self.model_id = model_id
        self.estimated_input_tokens = estimated_input_tokens
        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.reasoning_id = f"rs_{uuid.uuid4().hex}"
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.sequence = 0
        self.reasoning = ""
        self.text = ""
        self.reasoning_open = False
        self.reasoning_closed = False
        self.message_open = False
        self.message_closed = False
        self.output: list[dict[str, Any]] = []
        self.usage: dict[str, int] | None = None

    def _event(self, event_type: str, **payload: Any) -> bytes:
        event = {"type": event_type, "sequence_number": self.sequence, **payload}
        self.sequence += 1
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {encoded}\n\n".encode()

    def created(self) -> bytes:
        return self._event(
            "response.created",
            response={
                "id": self.response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "in_progress",
                "model": self.model_id,
                "output": [],
            },
        )

    def reasoning_delta(self, delta: str) -> list[bytes]:
        events: list[bytes] = []
        if not self.reasoning_open:
            self.reasoning_open = True
            events.extend(
                [
                    self._event(
                        "response.output_item.added",
                        output_index=len(self.output),
                        item={
                            "id": self.reasoning_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "summary": [],
                        },
                    ),
                    self._event(
                        "response.reasoning_summary_part.added",
                        item_id=self.reasoning_id,
                        output_index=len(self.output),
                        summary_index=0,
                        part={"type": "summary_text", "text": ""},
                    ),
                ]
            )
        self.reasoning += delta
        events.append(
            self._event(
                "response.reasoning_summary_text.delta",
                item_id=self.reasoning_id,
                output_index=len(self.output),
                summary_index=0,
                delta=delta,
            )
        )
        return events

    def close_reasoning(self) -> list[bytes]:
        if not self.reasoning_open or self.reasoning_closed:
            return []
        index = len(self.output)
        item = {
            "id": self.reasoning_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": self.reasoning}],
        }
        self.output.append(item)
        self.reasoning_closed = True
        return [
            self._event(
                "response.reasoning_summary_text.done",
                item_id=self.reasoning_id,
                output_index=index,
                summary_index=0,
                text=self.reasoning,
            ),
            self._event(
                "response.reasoning_summary_part.done",
                item_id=self.reasoning_id,
                output_index=index,
                summary_index=0,
                part={"type": "summary_text", "text": self.reasoning},
            ),
            self._event("response.output_item.done", output_index=index, item=item),
        ]

    def text_delta(self, delta: str) -> list[bytes]:
        events = self.close_reasoning()
        if not self.message_open:
            self.message_open = True
            index = len(self.output)
            events.extend(
                [
                    self._event(
                        "response.output_item.added",
                        output_index=index,
                        item={
                            "id": self.message_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    ),
                    self._event(
                        "response.content_part.added",
                        item_id=self.message_id,
                        output_index=index,
                        content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []},
                    ),
                ]
            )
        self.text += delta
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self.message_id,
                output_index=len(self.output),
                content_index=0,
                delta=delta,
                logprobs=[],
            )
        )
        return events

    def close_message(self) -> list[bytes]:
        if not self.message_open or self.message_closed:
            return []
        index = len(self.output)
        part = {"type": "output_text", "text": self.text, "annotations": []}
        item = {
            "id": self.message_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        self.output.append(item)
        self.message_closed = True
        return [
            self._event(
                "response.output_text.done",
                item_id=self.message_id,
                output_index=index,
                content_index=0,
                text=self.text,
                logprobs=[],
            ),
            self._event(
                "response.content_part.done",
                item_id=self.message_id,
                output_index=index,
                content_index=0,
                part=part,
            ),
            self._event("response.output_item.done", output_index=index, item=item),
        ]

    def set_usage(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        input_tokens = raw.get("prompt_tokens", raw.get("input_tokens"))
        output_tokens = raw.get("completion_tokens", raw.get("output_tokens"))
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens >= 0
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0
        ):
            self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}

    def complete(self) -> list[bytes]:
        events = self.close_reasoning()
        events.extend(self.close_message())
        if not self.text:
            raise AgentModelPlaneError("RUNTIME_PROVIDER_EMPTY_RESPONSE", status_code=502)
        usage = self.usage or {
            "input_tokens": self.estimated_input_tokens,
            "output_tokens": _estimate_tokens(self.text),
        }
        usage = {
            **usage,
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        self.usage = usage
        events.append(
            self._event(
                "response.completed",
                response={
                    "id": self.response_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "error": None,
                    "incomplete_details": None,
                    "model": self.model_id,
                    "output": self.output,
                    "usage": usage,
                },
            )
        )
        events.append(b"data: [DONE]\n\n")
        return events


@dataclass(frozen=True, slots=True)
class _NativeResponsesTerminal:
    event: bytes
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None


class _NativeResponsesStreamValidator:
    """Validate provider-native Responses before exposing terminal state."""

    def __init__(
        self,
        tool_aliases: Mapping[str, tuple[str, str]] | None = None,
        *,
        reasoning_visibility: str = "none",
        allow_tools: bool = False,
    ) -> None:
        self.last_sequence = -1
        self.seen_created = False
        self.terminal: _NativeResponsesTerminal | None = None
        self.tool_aliases = dict(tool_aliases or {})
        self.reasoning_visibility = reasoning_visibility
        self.allow_tools = allow_tools

    def _normalize_reasoning_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if self.reasoning_visibility != "stream" or event_type not in {
            "response.reasoning_text.delta",
            "response.reasoning_text.done",
        }:
            return
        normalized = event_type.replace(
            "response.reasoning_text",
            "response.reasoning_summary_text",
        )
        event["type"] = normalized
        event.setdefault("summary_index", 0)

    def _restore_tool_namespace(self, item: Any) -> None:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        alias = item.get("name")
        resolved = self.tool_aliases.get(alias) if isinstance(alias, str) else None
        if resolved is not None:
            namespace, name = resolved
            item["name"] = name
            item["namespace"] = namespace

    @staticmethod
    def _encoded(event_type: str, event: Mapping[str, Any]) -> bytes:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {payload}\n\n".encode()

    @staticmethod
    def _usage(response: Mapping[str, Any]) -> tuple[int, int]:
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_USAGE_INVALID", status_code=502)
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (input_tokens, output_tokens)
        ):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_USAGE_INVALID", status_code=502)
        return input_tokens, output_tokens

    def _reject_tool_item(self, event: Mapping[str, Any]) -> None:
        item = event.get("item")
        allowed = {None, "message", "reasoning"}
        if self.allow_tools:
            allowed.add("function_call")
        if isinstance(item, Mapping) and item.get("type") not in allowed:
            raise AgentModelPlaneError(
                "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                status_code=502,
            )

    def consume(self, payload: str) -> bytes | None:
        if payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            raise AgentModelPlaneError(
                "RUNTIME_PROVIDER_STREAM_INVALID",
                status_code=502,
            ) from None
        if not isinstance(event, dict):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        self._normalize_reasoning_event(event)
        event_type = event.get("type")
        sequence = event.get("sequence_number")
        if (
            not isinstance(event_type, str)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != self.last_sequence + 1
            or self.terminal is not None
        ):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        self.last_sequence = sequence
        if not self.seen_created:
            if event_type != "response.created":
                raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
            self.seen_created = True
        if event_type in {
            "error",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
        }:
            raise AgentModelPlaneError("RUNTIME_PROVIDER_REJECTED", status_code=502)
        if (
            any(marker in event_type for marker in ("function_call", "_search_call", "mcp_call"))
            and not self.allow_tools
        ):
            raise AgentModelPlaneError(
                "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                status_code=502,
            )
        self._restore_tool_namespace(event.get("item"))
        self._reject_tool_item(event)
        encoded = self._encoded(event_type, event)
        if event_type != "response.completed":
            return encoded
        response = event.get("response")
        if not isinstance(response, Mapping) or response.get("status") != "completed":
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                self._restore_tool_namespace(item)
                allowed = {"message", "reasoning"}
                if self.allow_tools:
                    allowed.add("function_call")
                if isinstance(item, Mapping) and item.get("type") not in allowed:
                    raise AgentModelPlaneError(
                        "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                        status_code=502,
                    )
        input_tokens, output_tokens = self._usage(response)
        response_id = response.get("id")
        self.terminal = _NativeResponsesTerminal(
            event=encoded,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=response_id if isinstance(response_id, str) else None,
        )
        return None

    def finish(self) -> _NativeResponsesTerminal:
        if not self.seen_created or self.terminal is None:
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INCOMPLETE", status_code=502)
        return self.terminal


class AgentModelPlane:
    def __init__(
        self,
        *,
        database: _Database,
        provider_service: Any,
        lease_signer: RuntimeModelLeaseSigner,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.database = database
        self.provider_service = provider_service
        self.lease_signer = lease_signer
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        self._owns_http_client = http_client is None

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def authorize_and_reserve(
        self,
        *,
        body: dict[str, Any],
        turn_metadata: dict[str, Any],
    ) -> _AuthorizedCall:
        lease_id_raw = turn_metadata.get("ai_platform_lease_id")
        signature = turn_metadata.get("ai_platform_lease_signature")
        try:
            lease_id = uuid.UUID(str(lease_id_raw))
        except (ValueError, TypeError, AttributeError):
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_INVALID", status_code=401) from None
        row = await self.database.fetchrow(
            """
            SELECT l.*, s.snapshot, s.snapshot_sha256
              FROM assistant_runtime_model_leases AS l
              JOIN assistant_runtime_snapshots AS s
                ON s.snapshot_id = l.snapshot_id
               AND s.run_id = l.run_id
               AND s.tenant_id = l.tenant_id
               AND s.user_id = l.user_id
               AND s.session_id = l.session_id
              JOIN assistant_runs AS run
                ON run.run_id = l.run_id
             WHERE l.lease_id = $1
               AND l.status = 'active'
               AND l.expires_at > NOW()
               AND run.status = 'running'
               AND run.engine = 'agent_runtime'
               AND NOT EXISTS (
                   SELECT 1
                     FROM assistant_runtime_snapshot_revocations AS revoked
                    WHERE revoked.snapshot_id = l.snapshot_id
               )
            """,
            lease_id,
        )
        if row is None:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_NOT_FOUND", status_code=401)
        data = dict(row)
        claims = RuntimeModelLeaseClaims(
            schema_version=str(data["schema_version"]),
            lease_id=str(data["lease_id"]),
            snapshot_id=str(data["snapshot_id"]),
            run_id=str(data["run_id"]),
            runtime_thread_id=str(data["runtime_thread_id"]),
            tenant_id=str(data["tenant_id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            provider_id=str(data["provider_id"]),
            model_id=str(data["model_id"]),
            capability_revision=int(data["capability_revision"]),
            issued_at_ms=_timestamp_ms(data["issued_at"]),
            expires_at_ms=_timestamp_ms(data["expires_at"]),
            nonce_sha256=str(data["nonce_sha256"]),
        )
        try:
            self.lease_signer.verify(str(signature or ""), claims)
        except RuntimeModelLeaseError as exc:
            raise AgentModelPlaneError(exc.code, status_code=401) from None

        expected_metadata = {
            "thread_id": claims.runtime_thread_id,
            "turn_id": claims.run_id,
            "ai_platform_scope_sha256": _runtime_scope_sha256(
                claims.tenant_id,
                claims.user_id,
                claims.session_id,
            ),
        }
        if any(
            str(turn_metadata.get(key) or "") != value for key, value in expected_metadata.items()
        ):
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
        if str(body.get("model") or "") != claims.model_id:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_MODEL_MISMATCH", status_code=403)
        snapshot = _runtime_snapshot(data.get("snapshot"))
        snapshot_hash = sha256(canonical_runtime_json(snapshot).encode()).hexdigest()
        if snapshot_hash != str(data["snapshot_sha256"]):
            raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_HASH_MISMATCH", status_code=503)

        estimated_input = _estimate_tokens(body.get("input"))
        limits = snapshot.get("limits") if isinstance(snapshot.get("limits"), dict) else {}
        requested_output = body.get("max_output_tokens")
        if not isinstance(requested_output, int) or isinstance(requested_output, bool):
            requested_output = int(limits.get("max_output_tokens") or 4096)
        requested_output = max(1, requested_output)
        pricing = snapshot.get("pricing") if isinstance(snapshot.get("pricing"), dict) else {}
        reserved_cost = _cost_microusd(
            estimated_input,
            requested_output,
            input_price_per_1k=float(pricing.get("input_price_per_1k") or 0),
            output_price_per_1k=float(pricing.get("output_price_per_1k") or 0),
        )
        call_id = uuid.uuid4()
        request_hash = sha256(canonical_runtime_json(body).encode()).hexdigest()
        try:
            await self.database.fetchrow(
                "SELECT reserve_assistant_runtime_model_call($1, $2, $3, $4, $5, $6)",
                call_id,
                lease_id,
                request_hash,
                estimated_input,
                requested_output,
                reserved_cost,
            )
        except Exception as exc:
            code = str(exc)
            if "MODEL_CALL_REPLAYED" in code:
                raise AgentModelPlaneError("RUNTIME_MODEL_CALL_REPLAYED", status_code=409) from None
            if "BUDGET_EXHAUSTED" in code:
                raise AgentModelPlaneError(
                    "RUNTIME_MODEL_LEASE_BUDGET_EXHAUSTED", status_code=429
                ) from None
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_REJECTED", status_code=403) from None
        return _AuthorizedCall(
            call_id=call_id,
            lease_id=lease_id,
            run_id=uuid.UUID(claims.run_id),
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            session_id=claims.session_id,
            model_id=claims.model_id,
            provider_id=claims.provider_id,
            provider_revision=str(data["provider_revision"]),
            snapshot=snapshot,
            estimated_input_tokens=estimated_input,
            reserved_output_tokens=requested_output,
        )

    async def stream(
        self,
        *,
        body: dict[str, Any],
        turn_metadata: dict[str, Any],
        authorized_call: _AuthorizedCall | None = None,
    ) -> AsyncIterator[bytes]:
        call = authorized_call or await self.authorize_and_reserve(
            body=body,
            turn_metadata=turn_metadata,
        )
        provider = await self.provider_service.get_runtime_provider_config(
            call.tenant_id,
            call.provider_id,
        )
        if _provider_revision(provider.get("updated_at")) != call.provider_revision:
            await self._fail_call(call.call_id, "provider_revision_changed", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_PROVIDER_REVISION_CHANGED", status_code=409)
        api_key = str(provider.get("api_key") or "")
        base_url = str(provider.get("runtime_base_url") or "")
        if not api_key or not base_url:
            await self._fail_call(call.call_id, "provider_unavailable", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_PROVIDER_UNAVAILABLE", status_code=503)
        snapshot_model = call.snapshot.get("model")
        profile = call.snapshot.get("capabilities")
        reasoning = call.snapshot.get("reasoning")
        if (
            not isinstance(snapshot_model, dict)
            or not isinstance(profile, dict)
            or not isinstance(reasoning, dict)
        ):
            await self._fail_call(call.call_id, "snapshot_invalid", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
        snapshot_parameters = _snapshot_parameters(call.snapshot)
        wire_protocol = str(snapshot_model.get("wire_protocol") or "")
        if wire_protocol == "responses_v1":
            try:
                async for chunk in self._stream_native_responses(
                    call=call,
                    body={**body, **snapshot_parameters},
                    profile=profile,
                    reasoning=reasoning,
                    api_key=api_key,
                    base_url=base_url,
                ):
                    yield chunk
            finally:
                api_key = ""
            return
        if wire_protocol != "chat_completions":
            await self._fail_call(call.call_id, "wire_protocol_unsupported", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_PROVIDER_WIRE_UNSUPPORTED", status_code=422)

        chat_body: dict[str, Any] = {
            "model": call.model_id,
            "messages": _responses_input_to_messages(body),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": call.reserved_output_tokens,
        }
        chat_body.update(snapshot_parameters)
        try:
            apply_reasoning_wire(
                chat_body, profile, str(reasoning.get("effective_option") or "auto")
            )
        except ReasoningWireError:
            await self._fail_call(call.call_id, "reasoning_wire_invalid", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_REASONING_WIRE_INVALID", status_code=422) from None

        await self.database.execute(
            """
            UPDATE assistant_runtime_model_calls
               SET status = 'dispatched', dispatched_at = NOW(), updated_at = NOW()
             WHERE call_id = $1 AND status = 'reserved'
            """,
            call.call_id,
        )
        projector = _ResponsesProjector(
            model_id=call.model_id,
            estimated_input_tokens=call.estimated_input_tokens,
        )
        provider_request_id: str | None = None
        try:
            async with self.http_client.stream(
                "POST",
                _chat_completions_url(base_url),
                headers=_provider_headers(profile, api_key),
                json=chat_body,
            ) as response:
                provider_request_id = response.headers.get("x-request-id")
                if response.status_code >= 400:
                    await response.aread()
                    await self._fail_call(
                        call.call_id,
                        f"provider_http_{response.status_code}",
                        dispatched=True,
                    )
                    raise AgentModelPlaneError("RUNTIME_PROVIDER_REJECTED", status_code=502)
                yield projector.created()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        raise AgentModelPlaneError(
                            "RUNTIME_PROVIDER_STREAM_INVALID", status_code=502
                        ) from None
                    if not isinstance(event, dict):
                        raise AgentModelPlaneError(
                            "RUNTIME_PROVIDER_STREAM_INVALID", status_code=502
                        )
                    projector.set_usage(event.get("usage"))
                    choices = event.get("choices")
                    if not isinstance(choices, list):
                        continue
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        if delta.get("tool_calls"):
                            raise AgentModelPlaneError(
                                "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                                status_code=502,
                            )
                        reasoning_delta = delta.get("reasoning_content")
                        if isinstance(reasoning_delta, str) and reasoning_delta:
                            for chunk in projector.reasoning_delta(reasoning_delta):
                                yield chunk
                        text_delta = delta.get("content")
                        if isinstance(text_delta, str) and text_delta:
                            for chunk in projector.text_delta(text_delta):
                                yield chunk
            terminal_chunks = projector.complete()
            assert projector.usage is not None
            usage = projector.usage
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            await self._complete_call(
                call=call,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider_request_id=provider_request_id,
            )
            for chunk in terminal_chunks:
                yield chunk
        except AgentModelPlaneError:
            await self._mark_unknown_if_dispatched(call.call_id)
            raise
        except BaseException:
            await self._mark_unknown_if_dispatched(call.call_id)
            raise
        finally:
            # Drop the local reference promptly; never retain tenant credentials
            # in caches, snapshots, exceptions, or telemetry.
            api_key = ""

    async def _stream_native_responses(
        self,
        *,
        call: _AuthorizedCall,
        body: dict[str, Any],
        profile: Mapping[str, Any],
        reasoning: Mapping[str, Any],
        api_key: str,
        base_url: str,
    ) -> AsyncIterator[bytes]:
        try:
            provider_body, tool_aliases = _native_responses_body(
                body,
                model_id=call.model_id,
                max_output_tokens=call.reserved_output_tokens,
                profile=profile,
                reasoning_option=str(reasoning.get("effective_option") or "auto"),
            )
        except ReasoningWireError:
            await self._fail_call(call.call_id, "reasoning_wire_invalid", dispatched=False)
            raise AgentModelPlaneError(
                "RUNTIME_REASONING_WIRE_INVALID",
                status_code=422,
            ) from None
        except AgentModelPlaneError:
            await self._fail_call(call.call_id, "responses_request_invalid", dispatched=False)
            raise

        await self.database.execute(
            """
            UPDATE assistant_runtime_model_calls
               SET status = 'dispatched', dispatched_at = NOW(), updated_at = NOW()
             WHERE call_id = $1 AND status = 'reserved'
            """,
            call.call_id,
        )
        reasoning_profile = profile.get("reasoning")
        reasoning_visibility = (
            str(reasoning_profile.get("visibility") or "none")
            if isinstance(reasoning_profile, Mapping)
            else "none"
        )
        validator = _NativeResponsesStreamValidator(
            tool_aliases,
            reasoning_visibility=reasoning_visibility,
            allow_tools=any(
                isinstance(tool, Mapping) and tool.get("type") == "function"
                for tool in provider_body.get("tools", [])
            ),
        )
        header_request_id: str | None = None
        try:
            async with self.http_client.stream(
                "POST",
                _responses_url(base_url),
                headers=_provider_headers(profile, api_key),
                json=provider_body,
            ) as response:
                header_request_id = response.headers.get("x-request-id")
                if response.status_code >= 400:
                    await response.aread()
                    await self._fail_call(
                        call.call_id,
                        f"provider_http_{response.status_code}",
                        dispatched=True,
                    )
                    raise AgentModelPlaneError("RUNTIME_PROVIDER_REJECTED", status_code=502)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    event = validator.consume(payload)
                    if event is not None:
                        yield event
            terminal = validator.finish()
            await self._complete_call(
                call=call,
                input_tokens=terminal.input_tokens,
                output_tokens=terminal.output_tokens,
                provider_request_id=terminal.provider_request_id or header_request_id,
            )
            yield terminal.event
            yield b"data: [DONE]\n\n"
        except AgentModelPlaneError:
            await self._mark_unknown_if_dispatched(call.call_id)
            raise
        except BaseException:
            await self._mark_unknown_if_dispatched(call.call_id)
            raise

    async def _complete_call(
        self,
        *,
        call: _AuthorizedCall,
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None,
    ) -> None:
        pricing = call.snapshot.get("pricing") or {}
        cost = _cost_microusd(
            input_tokens,
            output_tokens,
            input_price_per_1k=float(pricing.get("input_price_per_1k") or 0),
            output_price_per_1k=float(pricing.get("output_price_per_1k") or 0),
        )
        await self.database.fetchrow(
            "SELECT complete_assistant_runtime_model_call($1, $2, $3, $4, $5)",
            call.call_id,
            input_tokens,
            output_tokens,
            cost,
            provider_request_id,
        )

    async def _fail_call(self, call_id: uuid.UUID, code: str, *, dispatched: bool) -> None:
        status = "unknown" if dispatched else "failed"
        await self.database.execute(
            """
            UPDATE assistant_runtime_model_calls
               SET status = $2, error_code = $3, completed_at = NOW(), updated_at = NOW()
             WHERE call_id = $1 AND status IN ('reserved', 'dispatched')
            """,
            call_id,
            status,
            code,
        )

    async def _mark_unknown_if_dispatched(self, call_id: uuid.UUID) -> None:
        with contextlib.suppress(Exception):
            await self.database.execute(
                """
                UPDATE assistant_runtime_model_calls
                   SET status = 'unknown', error_code = 'stream_interrupted',
                       completed_at = NOW(), updated_at = NOW()
                 WHERE call_id = $1 AND status = 'dispatched'
                """,
                call_id,
            )


__all__ = ["AgentModelPlane", "AgentModelPlaneError"]
