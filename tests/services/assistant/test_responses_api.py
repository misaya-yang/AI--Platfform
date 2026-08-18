"""Strict upstream OpenAI Responses wire adapter contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from ai_gateway_core.models import ChatMessage
from assistant_service.core.agent.stream_helpers import merge_stream_tool_calls
from assistant_service.core.models.model_registry import ModelInfo, ModelProvider, ModelRegistry
from assistant_service.core.models.responses_api import (
    CHAT_COMPLETIONS_WIRE_PROTOCOL,
    RESPONSES_V1_WIRE_PROTOCOL,
    ResponsesAPIError,
    build_responses_request,
    iter_responses_stream,
    parse_responses_response,
)


def _event(event_type: str, **payload: Any) -> dict[str, Any]:
    return {"type": event_type, **payload}


def _response(response_id: str = "resp_1", **payload: Any) -> dict[str, Any]:
    return {"id": response_id, "object": "response", **payload}


async def _lines(events: list[dict[str, Any] | str]) -> AsyncIterator[str]:
    sequence_number = 0
    for event in events:
        if isinstance(event, str):
            yield f"data: {event}"
        else:
            sequenced_event = dict(event)
            sequenced_event.setdefault("sequence_number", sequence_number)
            sequence_number += 1
            yield f"data: {json.dumps(sequenced_event)}"
        yield ""


async def _collect(events: list[dict[str, Any] | str]):
    return [delta async for delta in iter_responses_stream(_lines(events))]


def _tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up one value",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


def _usage(
    input_tokens: int = 10,
    output_tokens: int = 4,
    *,
    cached_tokens: int | None = 3,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_tokens is not None:
        usage["input_tokens_details"] = {"cached_tokens": cached_tokens}
    return usage


def _completed_response(*, output: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return _response(
        status="completed",
        output=output
        if output is not None
        else [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Hello",
                        "annotations": [],
                    }
                ],
            }
        ],
        usage=_usage(),
    )


def test_request_uses_responses_shape_and_replays_tool_history() -> None:
    body = build_responses_request(
        model_id="gpt-test",
        messages=[
            ChatMessage(role="system", content="Be useful"),
            ChatMessage(role="user", content="Find it"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"query":"first"}',
                        },
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                content='{"value":1}',
                name="lookup",
                tool_call_id="call_1",
            ),
        ],
        temperature=0.2,
        max_output_tokens=512,
        tools=[_tool_schema()],
        stream=True,
        reasoning_effort="high",
    )

    assert set(body) == {
        "model",
        "input",
        "temperature",
        "stream",
        "store",
        "max_output_tokens",
        "tools",
        "reasoning",
    }
    assert body["store"] is False
    assert body["stream"] is True
    assert body["max_output_tokens"] == 512
    assert "messages" not in body
    assert "max_tokens" not in body
    assert body["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up one value",
            "parameters": _tool_schema()["function"]["parameters"],
            "strict": True,
        }
    ]
    assert body["input"][-2:] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"query":"first"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"value":1}',
        },
    ]


@pytest.mark.parametrize(
    ("messages", "match"),
    [
        (
            [
                ChatMessage(role="user", content="run"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "private-unpaired-call",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                ),
            ],
            "unpaired tool exchange",
        ),
        (
            [
                ChatMessage(role="user", content="run"),
                ChatMessage(
                    role="tool",
                    content="private-orphan-result",
                    name="lookup",
                    tool_call_id="private-orphan-call",
                ),
            ],
            "orphan tool result",
        ),
        (
            [
                ChatMessage(role="user", content="run"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "private-duplicate-call",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        },
                        {
                            "id": "private-duplicate-call",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        },
                    ],
                ),
            ],
            "duplicate tool call",
        ),
    ],
)
def test_request_rejects_invalid_tool_exchange_without_sensitive_error_text(
    messages: list[ChatMessage],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match) as exc_info:
        build_responses_request(
            model_id="gpt-test",
            messages=messages,
            temperature=0,
            max_output_tokens=100,
            tools=[_tool_schema()],
            stream=True,
        )

    error_text = str(exc_info.value)
    assert "private-" not in error_text
    assert "lookup" not in error_text


def test_nonstream_parser_normalizes_text_tools_and_usage() -> None:
    result = parse_responses_response(
        _completed_response(
            output=[
                {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "prefix "}],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{"query":"x"}',
                },
            ]
        )
    )

    assert result.content == "prefix "
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"query":"x"}'},
        }
    ]
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cached_input_tokens": 3,
    }


@pytest.mark.asyncio
async def test_registry_nonstream_posts_responses_endpoint_and_returns_usage() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, request=request, json=_completed_response())

    registry = ModelRegistry(use_default_models=False)
    registry.add_custom_model(ModelInfo("gpt-test", "GPT Test", ModelProvider.OPENAI))
    registry.configure_provider(
        ModelProvider.OPENAI,
        api_key="test-key",
        base_url="https://provider.test",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    registry._clients[ModelProvider.OPENAI] = httpx.AsyncClient(
        base_url="https://provider.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        content, usage = await registry.chat(
            "gpt-test",
            [ChatMessage(role="user", content="hello")],
            temperature=0.1,
            max_tokens=321,
        )
    finally:
        await registry.close()

    assert captured["path"] == "/v1/responses"
    assert captured["body"] == {
        "model": "gpt-test",
        "input": [{"role": "user", "content": "hello"}],
        "temperature": 0.1,
        "stream": False,
        "store": False,
        "max_output_tokens": 321,
    }
    assert content == "Hello"
    assert usage["cached_input_tokens"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "base_url", "expected_path"),
    [
        (ModelProvider.OPENAI, "https://provider.test/gateway/v1", "/gateway/v1/responses"),
        (
            ModelProvider.OPENAI,
            "https://provider.test/gateway/v1/responses",
            "/gateway/v1/responses",
        ),
        (
            ModelProvider.DASHSCOPE,
            "https://provider.test/compatible-mode/v1",
            "/compatible-mode/v1/responses",
        ),
        (
            ModelProvider.DASHSCOPE,
            "https://provider.test/compatible-mode/v1/responses",
            "/compatible-mode/v1/responses",
        ),
    ],
)
async def test_registry_normalizes_responses_base_url_without_duplicate_v1(
    provider: ModelProvider,
    base_url: str,
    expected_path: str,
) -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, request=request, json=_completed_response())

    registry = ModelRegistry(use_default_models=False)
    registry.add_custom_model(ModelInfo("model-test", "Model Test", provider))
    registry.configure_provider(
        provider,
        api_key="test-key",
        base_url=base_url,
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    registry._clients[provider] = httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        await registry.chat(
            "model-test",
            [ChatMessage(role="user", content="hello")],
        )
    finally:
        await registry.close()

    assert captured["path"] == expected_path


def _stream_events() -> list[dict[str, Any] | str]:
    return [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={"id": "rs_1", "type": "reasoning", "status": "in_progress"},
        ),
        _event(
            "response.reasoning_summary_text.delta",
            output_index=0,
            item_id="rs_1",
            delta="think",
        ),
        _event(
            "response.reasoning_summary_text.done",
            output_index=0,
            item_id="rs_1",
            text="thinking",
        ),
        _event(
            "response.output_item.done",
            output_index=0,
            item={"id": "rs_1", "type": "reasoning", "status": "completed"},
        ),
        _event(
            "response.output_item.added",
            output_index=1,
            item={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        ),
        _event(
            "response.content_part.added",
            output_index=1,
            content_index=0,
            item_id="msg_1",
            part={"type": "output_text", "text": ""},
        ),
        _event(
            "response.output_text.delta",
            output_index=1,
            content_index=0,
            item_id="msg_1",
            delta="Hello ",
        ),
        _event(
            "response.output_text.done",
            output_index=1,
            content_index=0,
            item_id="msg_1",
            text="Hello world",
        ),
        _event(
            "response.content_part.done",
            output_index=1,
            content_index=0,
            item_id="msg_1",
            part={"type": "output_text", "text": "Hello world"},
        ),
        _event(
            "response.output_item.done",
            output_index=1,
            item={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Hello world"}],
            },
        ),
        _event(
            "response.output_item.added",
            output_index=2,
            item={
                "id": "fc_1",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "",
            },
        ),
        _event(
            "response.function_call_arguments.delta",
            output_index=2,
            item_id="fc_1",
            delta='{"query":',
        ),
        _event(
            "response.function_call_arguments.delta",
            output_index=2,
            item_id="fc_1",
            delta='"x"}',
        ),
        _event(
            "response.function_call_arguments.done",
            output_index=2,
            item_id="fc_1",
            arguments='{"query":"x"}',
        ),
        _event(
            "response.output_item.done",
            output_index=2,
            item={
                "id": "fc_1",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"query":"x"}',
            },
        ),
        _event(
            "response.completed",
            response=_response(
                status="completed",
                output=[
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "status": "completed",
                    },
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Hello world"}],
                    },
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": '{"query":"x"}',
                    },
                ],
                usage=_usage(12, 8, cached_tokens=5),
            ),
        ),
        "[DONE]",
    ]


def _text_stream_events(
    *,
    text: str = "ok",
    usage: dict[str, Any] | None = None,
    completed_output: list[dict[str, Any]] | None = None,
    done_markers: int = 0,
) -> list[dict[str, Any] | str]:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    completed: dict[str, Any] = {
        "status": "completed",
        "usage": _usage() if usage is None else usage,
        "output": [message],
    }
    if completed_output is not None:
        completed["output"] = completed_output
    events: list[dict[str, Any] | str] = [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        ),
        _event(
            "response.output_text.delta",
            output_index=0,
            item_id="msg_1",
            delta=text,
        ),
        _event(
            "response.output_text.done",
            output_index=0,
            item_id="msg_1",
            text=text,
        ),
        _event("response.output_item.done", output_index=0, item=message),
        _event("response.completed", response=_response(**completed)),
    ]
    events.extend("[DONE]" for _ in range(done_markers))
    return events


ALIBABA_RESPONSES_SSE_FIXTURE = (
    "event: response.created",
    'data: {"type":"response.created","sequence_number":0,'
    '"response":{"id":"resp_ali_1","object":"response","status":"in_progress"}}',
    "event: response.output_item.added",
    'data: {"type":"response.output_item.added","sequence_number":1,"output_index":0,'
    '"item":{"id":"msg_ali_1","type":"message","role":"assistant",'
    '"status":"in_progress","content":[]}}',
    "event: response.output_text.delta",
    'data: {"type":"response.output_text.delta","sequence_number":2,"output_index":0,'
    '"item_id":"msg_ali_1","content_index":0,"delta":"Singapore"}',
    "event: response.output_text.done",
    'data: {"type":"response.output_text.done","sequence_number":3,"output_index":0,'
    '"item_id":"msg_ali_1","content_index":0,"text":"Singapore"}',
    "event: response.output_item.done",
    'data: {"type":"response.output_item.done","sequence_number":4,"output_index":0,'
    '"item":{"id":"msg_ali_1","type":"message","role":"assistant",'
    '"status":"completed","content":[{"type":"output_text","text":"Singapore"}]}}',
    "event: response.completed",
    'data: {"type":"response.completed","sequence_number":5,"response":'
    '{"id":"resp_ali_1","object":"response","status":"completed",'
    '"output":[{"id":"msg_ali_1","type":"message","role":"assistant",'
    '"status":"completed","content":[{"type":"output_text","text":"Singapore"}]}],'
    '"usage":{"input_tokens":8,"output_tokens":2,"total_tokens":10,'
    '"input_tokens_details":{"cached_tokens":0}}}}',
)


@pytest.mark.asyncio
async def test_registry_stream_maps_reasoning_text_tools_and_terminal_usage() -> None:
    captured: dict[str, Any] = {}
    wire_events: list[str] = []
    sequence_number = 0
    for event in _stream_events():
        if isinstance(event, str):
            wire_events.append(event)
        else:
            sequenced_event = dict(event)
            sequenced_event["sequence_number"] = sequence_number
            sequence_number += 1
            wire_events.append(json.dumps(sequenced_event))
    stream_content = "\n\n".join(f"data: {event}" for event in wire_events).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=stream_content,
        )

    registry = ModelRegistry(use_default_models=False)
    registry.add_custom_model(ModelInfo("qwen3.7-plus", "Qwen", ModelProvider.DASHSCOPE))
    base_url = "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode"
    registry.configure_provider(
        ModelProvider.DASHSCOPE,
        api_key="test-key",
        base_url=base_url,
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    registry._clients[ModelProvider.DASHSCOPE] = httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        deltas = [
            delta
            async for delta in registry.chat_stream(
                "qwen3.7-plus",
                [ChatMessage(role="user", content="hello")],
                max_tokens=512,
                tools=[_tool_schema()],
                thinking_level="enabled",
            )
        ]
    finally:
        await registry.close()

    assert captured["url"].endswith("/compatible-mode/v1/responses")
    assert captured["body"]["store"] is False
    assert captured["body"]["max_output_tokens"] == 512
    assert captured["body"]["enable_thinking"] is True
    assert "messages" not in captured["body"]

    assert "".join(delta.thinking_content or "" for delta in deltas) == "thinking"
    assert "".join(delta.content for delta in deltas) == "Hello world"
    tool_calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    anonymous_counter = 0
    for delta in deltas:
        if delta.tool_calls:
            anonymous_counter = merge_stream_tool_calls(
                delta.tool_calls,
                tool_calls,
                order,
                anonymous_counter,
            )
    assert order == ["idx:0"]
    assert tool_calls["idx:0"]["id"] == "call_1"
    assert tool_calls["idx:0"]["function"] == {
        "name": "lookup",
        "arguments": '{"query":"x"}',
    }
    terminal = [delta for delta in deltas if delta.finish_reason]
    assert len(terminal) == 1
    assert terminal[0].finish_reason == "tool_calls"
    assert terminal[0].usage == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cached_input_tokens": 5,
    }


@pytest.mark.asyncio
async def test_alibaba_event_lines_and_terminal_without_done_are_supported() -> None:
    async def recorded_lines() -> AsyncIterator[str]:
        for line in ALIBABA_RESPONSES_SSE_FIXTURE:
            yield line
            if line.startswith("data:"):
                yield ""

    deltas = [delta async for delta in iter_responses_stream(recorded_lines())]

    assert "".join(delta.content for delta in deltas) == "Singapore"
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].usage == {
        "input_tokens": 8,
        "output_tokens": 2,
        "total_tokens": 10,
        "cached_input_tokens": 0,
    }


@pytest.mark.asyncio
async def test_stream_rejects_empty_success() -> None:
    events = [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.completed",
            response=_response(status="completed", output=[], usage=_usage()),
        ),
    ]

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == "empty_response_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "error_type"),
    [
        (
            [
                _event(
                    "response.created",
                    sequence_number=True,
                    response=_response(status="in_progress"),
                )
            ],
            "invalid_sequence_number",
        ),
        (
            [
                _event(
                    "response.created",
                    sequence_number=-1,
                    response=_response(status="in_progress"),
                )
            ],
            "invalid_sequence_number",
        ),
        (
            [
                _event(
                    "response.created",
                    sequence_number=2,
                    response=_response(status="in_progress"),
                ),
                _event(
                    "response.in_progress",
                    sequence_number=2,
                    response=_response(status="in_progress"),
                ),
            ],
            "invalid_event_sequence",
        ),
        (
            [
                _event(
                    "response.created",
                    sequence_number=2,
                    response=_response(status="in_progress"),
                ),
                _event(
                    "response.in_progress",
                    sequence_number=1,
                    response=_response(status="in_progress"),
                ),
            ],
            "invalid_event_sequence",
        ),
    ],
)
async def test_stream_rejects_invalid_event_sequences(
    events: list[dict[str, Any] | str],
    error_type: str,
) -> None:
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == error_type


@pytest.mark.asyncio
async def test_stream_requires_sequence_number() -> None:
    async def unsequenced() -> AsyncIterator[str]:
        yield 'data: {"type":"response.created","response":{"id":"resp_1"}}'

    with pytest.raises(ResponsesAPIError) as exc_info:
        await anext(iter_responses_stream(unsequenced()))
    assert exc_info.value.error_type == "invalid_sequence_number"


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "second"), [(1, None), (0, 2)])
async def test_stream_rejects_sequence_start_or_gap(
    first: int,
    second: int | None,
) -> None:
    events = [
        _event(
            "response.created",
            sequence_number=first,
            response=_response(status="in_progress"),
        )
    ]
    if second is not None:
        events.append(
            _event(
                "response.in_progress",
                sequence_number=second,
                response=_response(status="in_progress"),
            )
        )

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)

    assert exc_info.value.error_type == "invalid_event_sequence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tail", "error_type"),
    [
        (
            _event(
                "response.output_text.delta",
                output_index=0,
                item_id="msg_1",
                delta="late",
            ),
            "event_after_text_done",
        ),
        (
            _event(
                "response.output_text.done",
                output_index=0,
                item_id="msg_1",
                text="ok",
            ),
            "text_rebinding",
        ),
    ],
)
async def test_stream_rejects_text_event_after_text_done(
    tail: dict[str, Any],
    error_type: str,
) -> None:
    events = _text_stream_events()[:4] + [tail]
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == error_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tail", "error_type"),
    [
        (
            _event(
                "response.reasoning_summary_text.delta",
                output_index=0,
                item_id="rs_1",
                delta="late",
            ),
            "event_after_reasoning_done",
        ),
        (
            _event(
                "response.reasoning_summary_text.done",
                output_index=0,
                item_id="rs_1",
                text="thought",
            ),
            "reasoning_rebinding",
        ),
    ],
)
async def test_stream_rejects_reasoning_event_after_done(
    tail: dict[str, Any],
    error_type: str,
) -> None:
    events = [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={"id": "rs_1", "type": "reasoning", "status": "in_progress"},
        ),
        _event(
            "response.reasoning_summary_text.done",
            output_index=0,
            item_id="rs_1",
            text="thought",
        ),
        tail,
    ]
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == error_type


@pytest.mark.asyncio
async def test_stream_rejects_message_output_item_rebinding() -> None:
    events = _text_stream_events()[:4]
    events.append(
        _event(
            "response.output_item.done",
            output_index=0,
            item={
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "changed"}],
            },
        )
    )
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == "text_rebinding"


@pytest.mark.asyncio
async def test_stream_rejects_completed_output_rebinding() -> None:
    changed_message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "changed"}],
    }
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(_text_stream_events(completed_output=[changed_message]))
    assert exc_info.value.error_type == "text_rebinding"


@pytest.mark.asyncio
async def test_output_item_done_safely_reconciles_missing_text_suffix() -> None:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Hello"}],
    }
    events = [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={"id": "msg_1", "type": "message", "content": []},
        ),
        _event(
            "response.output_text.delta",
            output_index=0,
            item_id="msg_1",
            delta="Hel",
        ),
        _event("response.output_item.done", output_index=0, item=message),
        _event(
            "response.completed",
            response=_response(status="completed", output=[message], usage=_usage()),
        ),
    ]

    deltas = await _collect(events)
    assert "".join(delta.content for delta in deltas) == "Hello"


@pytest.mark.asyncio
async def test_stream_rejects_function_output_item_rebinding() -> None:
    events = [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "",
            },
        ),
        _event(
            "response.function_call_arguments.done",
            output_index=0,
            item_id="fc_1",
            arguments="{}",
        ),
        _event(
            "response.output_item.done",
            output_index=0,
            item={
                "id": "fc_1",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_rebound",
                "name": "lookup",
                "arguments": "{}",
            },
        ),
    ]
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == "function_call_rebinding"


@pytest.mark.asyncio
async def test_stream_rejects_duplicate_done_marker() -> None:
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(_text_stream_events(done_markers=2))
    assert exc_info.value.error_type == "duplicate_done"


@pytest.mark.parametrize("status", ["failed", "incomplete", "queued"])
def test_nonstream_rejects_noncompleted_status(status: str) -> None:
    with pytest.raises(ResponsesAPIError):
        parse_responses_response(_response(status=status, output=[], usage={}))


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
        {"input_tokens": 1.0, "output_tokens": 1, "total_tokens": 2},
        {"input_tokens": -1, "output_tokens": 1, "total_tokens": 0},
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 3},
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 2},
        },
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": False},
        },
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": "bad",
        },
    ],
)
def test_nonstream_rejects_malformed_usage(usage: dict[str, Any]) -> None:
    response = _completed_response()
    response["usage"] = usage
    with pytest.raises(ResponsesAPIError) as exc_info:
        parse_responses_response(response)
    assert exc_info.value.error_type == "invalid_usage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 3},
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 2},
        },
        {"input_tokens": 1, "output_tokens": -1, "total_tokens": 0},
    ],
)
async def test_stream_rejects_malformed_usage(usage: dict[str, Any]) -> None:
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(_text_stream_events(usage=usage))
    assert exc_info.value.error_type == "invalid_usage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "error_type"),
    [
        ([_event("response.failed", response=_response(status="failed"))], "response_failed"),
        (
            [_event("response.incomplete", response=_response(status="incomplete"))],
            "response_incomplete",
        ),
        ([_event("error", error={"message": "private"})], "provider_error"),
        (
            [
                _event("response.created", response=_response(status="in_progress")),
                _event(
                    "response.function_call_arguments.delta",
                    output_index=0,
                    item_id="fc_orphan",
                    delta="{}",
                ),
            ],
            "orphan_output_event",
        ),
        (
            [
                _event("response.created", response=_response(status="in_progress")),
                _event(
                    "response.output_item.added",
                    output_index=0,
                    item={
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": "",
                    },
                ),
                _event(
                    "response.output_item.added",
                    output_index=0,
                    item={
                        "id": "fc_2",
                        "type": "function_call",
                        "call_id": "call_2",
                        "name": "lookup",
                        "arguments": "",
                    },
                ),
            ],
            "output_item_rebinding",
        ),
        (
            [
                _event("response.created", response=_response(status="in_progress")),
                _event(
                    "response.output_item.added",
                    output_index=0,
                    item={"id": "msg_1", "type": "message", "content": []},
                ),
                _event(
                    "response.output_text.done",
                    output_index=0,
                    item_id="msg_1",
                    text="ok",
                ),
                _event(
                    "response.output_item.done",
                    output_index=0,
                    item={
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                ),
                _event(
                    "response.completed",
                    response=_response(
                        status="completed",
                        output=[
                            {
                                "id": "msg_1",
                                "type": "message",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "ok"}],
                            }
                        ],
                        usage=_usage(),
                    ),
                ),
                _event("response.in_progress", response=_response(status="in_progress")),
            ],
            "event_after_terminal",
        ),
        (
            [_event("response.created", response=_response(status="in_progress"))],
            "incomplete_response",
        ),
        (["[DONE]"], "incomplete_response"),
    ],
)
async def test_stream_fail_closed_contracts(
    events: list[dict[str, Any] | str],
    error_type: str,
) -> None:
    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == error_type
    assert "private" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_stream_rejects_incomplete_function_lifecycle() -> None:
    events = [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "",
            },
        ),
        _event(
            "response.completed",
            response=_response(status="completed", usage={}),
        ),
    ]

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)
    assert exc_info.value.error_type == "incomplete_output_item"


def test_wire_protocol_is_opt_in_and_provider_scoped() -> None:
    registry = ModelRegistry(use_default_models=False)
    registry.configure_provider(ModelProvider.OPENAI, api_key="test-key")
    registry.configure_provider(ModelProvider.DASHSCOPE, api_key="test-key")

    assert registry._configs[ModelProvider.OPENAI].wire_protocol == (CHAT_COMPLETIONS_WIRE_PROTOCOL)
    assert registry._configs[ModelProvider.DASHSCOPE].wire_protocol == (
        CHAT_COMPLETIONS_WIRE_PROTOCOL
    )

    with pytest.raises(ValueError, match="Unsupported provider wire protocol"):
        registry.configure_provider(
            ModelProvider.OPENAI,
            api_key="test-key",
            wire_protocol="responses",
        )
    with pytest.raises(ValueError, match="does not support"):
        registry.configure_provider(
            ModelProvider.ANTHROPIC,
            api_key="test-key",
            wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
        )


def test_qwen_default_request_remains_chat_completions_shape() -> None:
    registry = ModelRegistry(use_default_models=False)
    registry.configure_provider(ModelProvider.DASHSCOPE, api_key="test-key")

    body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="hello")],
        max_tokens=256,
        stream=True,
        thinking_level="enabled",
    )

    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 256
    assert body["stream_options"] == {"include_usage": True}
    assert body["enable_thinking"] is True
    assert "input" not in body
    assert "store" not in body


def test_qwen_explicit_thinking_off_is_forwarded_for_both_wire_protocols() -> None:
    registry = ModelRegistry(use_default_models=False)
    registry.configure_provider(ModelProvider.DASHSCOPE, api_key="test-key")

    messages = [ChatMessage(role="user", content="summarize the completed tool result")]
    chat_body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        messages,
        max_tokens=512,
        stream=True,
        thinking_level="off",
    )

    registry.configure_provider(
        ModelProvider.DASHSCOPE,
        api_key="test-key",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    responses_body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        messages,
        max_tokens=512,
        stream=True,
        thinking_level="off",
    )

    assert chat_body["enable_thinking"] is False
    assert chat_body["max_tokens"] == 512
    assert responses_body["enable_thinking"] is False
    assert responses_body["max_output_tokens"] == 512


def test_responses_native_search_uses_provider_specific_tool_shape() -> None:
    registry = ModelRegistry(use_default_models=False)
    registry.configure_provider(
        ModelProvider.DASHSCOPE,
        api_key="test-key",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    registry.configure_provider(
        ModelProvider.OPENAI,
        api_key="test-key",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )

    dashscope_body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="search")],
        tools=[_tool_schema()],
        native_search_config={"enable_search": True},
    )
    openai_body = registry._build_request_body(
        ModelProvider.OPENAI,
        "gpt-test",
        [ChatMessage(role="user", content="search")],
        tools=[_tool_schema()],
        native_search_config={"enable_search": True},
    )

    assert dashscope_body["tools"][-1] == {"type": "web_search"}
    assert "enable_search" not in dashscope_body
    assert "extra_body" not in dashscope_body
    assert "search_options" not in dashscope_body
    # OpenAI search remains disabled because its capability table does not
    # advertise a Responses-native search tool contract yet.
    assert openai_body["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up one value",
            "parameters": _tool_schema()["function"]["parameters"],
            "strict": True,
        }
    ]


@pytest.mark.asyncio
async def test_nonstream_function_call_is_not_silently_dropped() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_completed_response(
                output=[
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": '{"query":"x"}',
                    }
                ]
            ),
        )

    registry = ModelRegistry(use_default_models=False)
    registry.add_custom_model(ModelInfo("gpt-test", "GPT Test", ModelProvider.OPENAI))
    registry.configure_provider(
        ModelProvider.OPENAI,
        api_key="test-key",
        base_url="https://provider.test",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    registry._clients[ModelProvider.OPENAI] = httpx.AsyncClient(
        base_url="https://provider.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ResponsesAPIError) as exc_info:
            await registry.chat(
                "gpt-test",
                [ChatMessage(role="user", content="use a tool")],
                tools=[_tool_schema()],
            )
    finally:
        await registry.close()

    assert exc_info.value.error_type == "nonstream_tool_calls_unsupported"


def _web_search_item(*, source_url: str = "https://weather.example/singapore") -> dict[str, Any]:
    return {
        "id": "ws_1",
        "type": "web_search_call",
        "status": "completed",
        "action": {
            "type": "search",
            "queries": ["Singapore weather"],
            "sources": [{"type": "url", "url": source_url}],
        },
    }


def _web_search_stream_events() -> list[dict[str, Any] | str]:
    web_item = _web_search_item()
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Sunny"}],
    }
    return [
        _event("response.created", response=_response(status="in_progress")),
        _event(
            "response.output_item.added",
            output_index=0,
            item={"id": "ws_1", "type": "web_search_call", "status": "in_progress"},
        ),
        _event("response.web_search_call.in_progress", output_index=0, item_id="ws_1"),
        _event("response.web_search_call.searching", output_index=0, item_id="ws_1"),
        _event("response.web_search_call.completed", output_index=0, item_id="ws_1"),
        _event("response.output_item.done", output_index=0, item=web_item),
        _event(
            "response.output_item.added",
            output_index=1,
            item={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        ),
        _event(
            "response.output_text.delta",
            output_index=1,
            item_id="msg_1",
            delta="Sunny",
        ),
        _event(
            "response.output_text.done",
            output_index=1,
            item_id="msg_1",
            text="Sunny",
        ),
        _event("response.output_item.done", output_index=1, item=message),
        _event(
            "response.completed",
            response=_response(
                status="completed",
                output=[web_item, message],
                usage=_usage(),
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_stream_accepts_native_web_search_lifecycle_without_client_tool_call() -> None:
    deltas = await _collect(_web_search_stream_events())

    assert "".join(delta.content for delta in deltas) == "Sunny"
    assert all(delta.tool_calls is None for delta in deltas)
    assert deltas[-1].finish_reason == "stop"


def test_nonstream_accepts_native_web_search_output_without_client_tool_call() -> None:
    result = parse_responses_response(
        _completed_response(
            output=[
                _web_search_item(),
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Sunny"}],
                },
            ]
        )
    )

    assert result.content == "Sunny"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_rejects_native_web_search_status_regression() -> None:
    events = _web_search_stream_events()
    events[3], events[4] = events[4], events[3]

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)

    assert exc_info.value.error_type == "invalid_server_tool_lifecycle"


@pytest.mark.asyncio
async def test_stream_rejects_native_web_search_added_with_terminal_status() -> None:
    events = _web_search_stream_events()
    added_item = events[1]["item"]
    assert isinstance(added_item, dict)
    added_item["status"] = "completed"

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)

    assert exc_info.value.error_type == "invalid_server_tool_lifecycle"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"queries": [42]},
        {"sources": [{"type": "url", "url": "javascript:alert(1)"}]},
    ],
)
async def test_stream_rejects_malformed_native_web_search_output(
    mutation: dict[str, Any],
) -> None:
    events = _web_search_stream_events()
    done_item = events[5]["item"]
    assert isinstance(done_item, dict)
    action = done_item["action"]
    assert isinstance(action, dict)
    action.update(mutation)

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)

    assert exc_info.value.error_type == "invalid_server_tool_output"


@pytest.mark.asyncio
async def test_stream_rejects_native_web_search_completed_output_rebinding() -> None:
    events = _web_search_stream_events()
    completed_response = events[-1]["response"]
    assert isinstance(completed_response, dict)
    output = completed_response["output"]
    assert isinstance(output, list)
    output[0] = _web_search_item(source_url="https://changed.example/result")

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)

    assert exc_info.value.error_type == "server_tool_rebinding"


@pytest.mark.asyncio
async def test_stream_requires_completed_response_output_for_terminal_binding() -> None:
    events = _text_stream_events()
    completed_response = events[-1]["response"]
    assert isinstance(completed_response, dict)
    completed_response.pop("output")

    with pytest.raises(ResponsesAPIError) as exc_info:
        await _collect(events)

    assert exc_info.value.error_type == "invalid_completed_output"


@pytest.mark.asyncio
async def test_stream_cancellation_propagates_without_terminal_success() -> None:
    async def cancelled_lines() -> AsyncIterator[str]:
        yield (
            'data: {"type":"response.created","sequence_number":0,'
            '"response":{"id":"resp_1","status":"in_progress"}}'
        )
        raise asyncio.CancelledError

    stream = iter_responses_stream(cancelled_lines())
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)


@pytest.mark.asyncio
async def test_registry_responses_http_error_redacts_body_prompt_and_key() -> None:
    api_key = "test-secret-api-key"
    prompt = "test-private-prompt"
    provider_body = "test-private-provider-body"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, content=provider_body.encode())

    registry = ModelRegistry(use_default_models=False)
    registry.add_custom_model(ModelInfo("gpt-test", "GPT Test", ModelProvider.OPENAI))
    registry.configure_provider(
        ModelProvider.OPENAI,
        api_key=api_key,
        base_url="https://provider.test/v1",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    registry._clients[ModelProvider.OPENAI] = httpx.AsyncClient(
        base_url="https://provider.test/v1",
        headers=registry._build_headers(ModelProvider.OPENAI, api_key),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            async for _ in registry.chat_stream(
                "gpt-test",
                [ChatMessage(role="user", content=prompt)],
            ):
                pass
    finally:
        await registry.close()

    rendered = str(exc_info.value)
    assert api_key not in rendered
    assert prompt not in rendered
    assert provider_body not in rendered
    assert exc_info.value.response.content == b""
