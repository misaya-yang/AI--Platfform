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
import inspect
import json
import math
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ai_gateway_core.logging import get_logger, log_internal_exception
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...auth import UserContext, get_user_context
from ...core.assistant_service import AssistantConfig, AssistantStreamEvent, RAGMode
from ...core.tool_invoker import CapabilityAllowlist
from ..deps import get_assistant_service, get_model_registry
from .responses_projector import (
    ResponsesIngressError as ResponsesIngressError,
)
from .responses_projector import (
    ResponsesStreamProjector as ResponsesStreamProjector,
)

# Preserve the historical dotted identities used by repr, pickling, and
# diagnostics while the implementations live in the focused projector module.
ResponsesIngressError.__module__ = __name__
ResponsesStreamProjector.__module__ = __name__

router = APIRouter()
logger = get_logger(__name__)

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


def _sse(event: dict[str, Any]) -> str:
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def _close_async_iterator(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.responses.stream_close_failed",
                exc,
            )


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
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.responses.stream.startup_failed",
                exc,
            )
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
    except Exception as exc:
        log_internal_exception(
            logger,
            "assistant.responses.stream.failed",
            exc,
        )
        if not projector.terminal:
            for event in projector.fail(code="server_error"):
                yield event
    finally:
        await _close_async_iterator(source)
        clear = getattr(assistant, "clear_session_runtime_state", None)
        if callable(clear):
            try:
                cleared = clear(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
                if inspect.isawaitable(cleared):
                    await cleared
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.responses.runtime_state_cleanup_failed",
                    exc,
                )


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
    except Exception as exc:
        log_internal_exception(logger, "assistant.responses.model_lookup_failed", exc)
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
    except Exception as exc:
        log_internal_exception(
            logger,
            "assistant.responses.provider_readiness_failed",
            exc,
        )
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
