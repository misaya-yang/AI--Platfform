from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from ai_gateway_core.agents import RuntimeModelLeaseSigner
from ai_gateway_core.models import get_builtin_model_capabilities

from src.services.agent_runtime.model_plane import (
    AgentModelPlane,
    AgentModelPlaneError,
    _AuthorizedCall,
    _native_responses_body,
    _NativeResponsesStreamValidator,
    _responses_input_to_messages,
    _runtime_snapshot,
)


def test_runtime_snapshot_accepts_asyncpg_json_text_and_rejects_non_objects() -> None:
    assert _runtime_snapshot('{"schema_version":"agent-runtime-snapshot/v1"}') == {
        "schema_version": "agent-runtime-snapshot/v1"
    }
    with pytest.raises(AgentModelPlaneError, match="SNAPSHOT_INVALID"):
        _runtime_snapshot("[]")


def test_dynamic_tool_transcript_accepts_one_pair_and_rejects_duplicate_call() -> None:
    body = {
        "input": [
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "Knowledge result"},
        ]
    }
    assert _responses_input_to_messages(body)[-1]["content"] == "Knowledge result"
    duplicate = {
        "input": [
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"},
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"},
        ]
    }
    with pytest.raises(AgentModelPlaneError, match="TRANSCRIPT"):
        _responses_input_to_messages(duplicate)


@pytest.mark.parametrize(
    "raw_input",
    [
        [{"type": "function_call_output", "call_id": "orphan", "output": "x"}],
        [{"type": "function_call", "call_id": "open", "name": "read", "arguments": "{}"}],
        [
            {"type": "function_call", "call_id": "call-1", "name": "other", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "x"},
        ],
    ],
)
def test_native_responses_rejects_invalid_tool_transcript_before_serialization(
    raw_input: list[dict[str, str]],
) -> None:
    profile = _profile()
    with pytest.raises(AgentModelPlaneError, match="TRANSCRIPT"):
        _native_responses_body(
            {
                "input": raw_input,
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "description": "Read one value.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
            model_id="qwen3.7-plus",
            max_output_tokens=256,
            profile=profile,
            reasoning_option="minimal",
        )


def test_fake_responses_tool_call_then_capability_result_can_start_next_model_round() -> None:
    validator = _NativeResponsesStreamValidator(allow_tools=True)
    validator.consume(
        json.dumps(
            {"type": "response.created", "sequence_number": 0, "response": {"id": "r1"}}
        )
    )
    validator.consume(
        json.dumps(
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "item": {
                    "id": "call-1",
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "search_knowledge_base",
                    "arguments": "{\"query\":\"transformer\"}",
                },
            }
        )
    )
    validator.consume(
        json.dumps(
            {
                "type": "response.completed",
                "sequence_number": 2,
                "response": {
                    "id": "r1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "search_knowledge_base",
                            "arguments": "{\"query\":\"transformer\"}",
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            }
        )
    )
    assert validator.finish().output_tokens == 2
    messages = _responses_input_to_messages(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "search_knowledge_base",
                    "arguments": "{\"query\":\"transformer\"}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Knowledge result: attention.",
                },
            ]
        }
    )
    assert messages[-1]["content"] == "Knowledge result: attention."


class _Database:
    def __init__(self) -> None:
        self.operations: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        del query
        self.operations.append(("execute", args))
        return "UPDATE 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        del query
        self.operations.append(("fetchrow", args))
        return {"ok": True}


class _ProviderService:
    async def get_runtime_provider_config(
        self,
        tenant_id: str,
        provider_id: str,
    ) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert provider_id == "dashscope"
        return {
            "updated_at": "provider-revision-1",
            "api_key": "provider-secret",
            "runtime_base_url": "https://dashscope.example/compatible-mode/v1",
        }


def _profile() -> dict[str, Any]:
    profile = get_builtin_model_capabilities("dashscope", "qwen3.7-plus")
    assert profile is not None
    return profile


def _call(profile: dict[str, Any], *, temperature: float | None = None) -> _AuthorizedCall:
    snapshot: dict[str, Any] = {
        "model": {"wire_protocol": "responses_v1"},
        "capabilities": profile,
        "reasoning": {"effective_option": "minimal"},
        "pricing": {"input_price_per_1k": 0.001, "output_price_per_1k": 0.002},
    }
    if temperature is not None:
        snapshot["parameters"] = {"temperature": temperature}
    return _AuthorizedCall(
        call_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        model_id="qwen3.7-plus",
        provider_id="dashscope",
        provider_revision="provider-revision-1",
        snapshot=snapshot,
        estimated_input_tokens=4,
        reserved_output_tokens=256,
    )


def _event(sequence: int, event_type: str, **payload: Any) -> str:
    value = {"type": event_type, "sequence_number": sequence, **payload}
    return f"data: {json.dumps(value, separators=(',', ':'))}\n\n"


def _native_stream() -> bytes:
    message = {
        "id": "msg_1",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "你好", "annotations": []}],
    }
    chunks = [
        _event(
            0,
            "response.created",
            response={"id": "resp_1", "status": "queued", "output": []},
        ),
        _event(
            1,
            "response.in_progress",
            response={"id": "resp_1", "status": "in_progress", "output": []},
        ),
        _event(
            2,
            "response.output_item.added",
            output_index=0,
            item={"id": "msg_1", "type": "message", "status": "in_progress"},
        ),
        _event(
            3,
            "response.output_text.delta",
            item_id="msg_1",
            output_index=0,
            content_index=0,
            delta="你好",
        ),
        _event(4, "response.output_item.done", output_index=0, item=message),
        _event(
            5,
            "response.completed",
            response={
                "id": "resp_1",
                "status": "completed",
                "output": [message],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 3,
                    "total_tokens": 14,
                    "input_tokens_details": {"cached_tokens": 7},
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            },
        ),
    ]
    return "".join(chunks).encode()


def test_qwen_tool_adapter_flattens_namespaces_without_prompt_routing() -> None:
    body, aliases = _native_responses_body(
        {
            "input": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "namespace",
                    "name": "skills",
                    "description": "Skill tools.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "read",
                            "description": "Read a skill.",
                            "parameters": {"type": "object", "properties": {}},
                            "strict": False,
                        }
                    ],
                },
                {"type": "web_search"},
            ],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=_profile(),
        reasoning_option="minimal",
    )

    function_tool = body["tools"][0]
    assert function_tool["type"] == "function"
    assert function_tool["name"].startswith("ns_")
    assert aliases[function_tool["name"]] == ("skills", "read")
    assert body["tools"][1] == {"type": "web_search"}


def test_web_search_alone_does_not_authorize_function_transcript() -> None:
    with pytest.raises(AgentModelPlaneError, match="TOOLS_NOT_ENABLED"):
        _native_responses_body(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "search_knowledge_base",
                        "arguments": "{}",
                    }
                ],
                "tools": [{"type": "web_search"}],
            },
            model_id="qwen3.7-plus",
            max_output_tokens=128,
            profile=_profile(),
            reasoning_option="minimal",
        )


def test_native_responses_projects_only_profile_visible_reasoning() -> None:
    raw_event = json.dumps(
        {
            "type": "response.reasoning_text.delta",
            "sequence_number": 1,
            "item_id": "reasoning_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "先分析",
        },
        separators=(",", ":"),
    )
    created_event = json.dumps(
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_1", "status": "queued", "output": []},
        },
        separators=(",", ":"),
    )

    visible = _NativeResponsesStreamValidator(reasoning_visibility="stream")
    assert visible.consume(created_event) is not None
    visible_chunk = visible.consume(raw_event)
    assert visible_chunk is not None
    assert b"response.reasoning_summary_text.delta" in visible_chunk
    assert b'"summary_index":0' in visible_chunk

    hidden = _NativeResponsesStreamValidator(reasoning_visibility="none")
    assert hidden.consume(created_event) is not None
    hidden_chunk = hidden.consume(raw_event)
    assert hidden_chunk is not None
    assert b"response.reasoning_text.delta" in hidden_chunk
    assert b"response.reasoning_summary_text.delta" not in hidden_chunk


@pytest.mark.asyncio
async def test_qwen_native_responses_is_default_wire_and_completes_before_terminal() -> None:
    captured: dict[str, Any] = {"http_calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["http_calls"] += 1
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream", "x-request-id": "req-header"},
            content=_native_stream(),
        )

    database = _Database()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = AgentModelPlane(
        database=database,
        provider_service=_ProviderService(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        http_client=client,
    )
    profile = _profile()
    assert profile["wire_protocols"]["preferred"] == "responses_v1"
    call = _call(profile, temperature=0.2)
    try:
        chunks = [
            chunk
            async for chunk in plane.stream(
                body={
                    "model": "qwen3.7-plus",
                    "input": [{"role": "user", "content": "你好"}],
                    "stream": True,
                    "store": True,
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup",
                            "description": "Look up one value.",
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                            "strict": True,
                            "defer_loading": False,
                        }
                    ],
                },
                turn_metadata={},
                authorized_call=call,
            )
        ]
    finally:
        await client.aclose()

    assert captured["http_calls"] == 1
    assert captured["url"].endswith("/compatible-mode/v1/responses")
    assert captured["headers"]["x-dashscope-session-cache"] == "enable"
    assert captured["body"] == {
        "model": "qwen3.7-plus",
        "input": [{"role": "user", "content": "你好"}],
        "stream": True,
        "store": False,
        "max_output_tokens": 256,
        "temperature": 0.2,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": "Look up one value.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "strict": True,
            }
        ],
        "tool_choice": "auto",
        "reasoning": {"effort": "minimal"},
    }
    terminal_index = next(
        index for index, chunk in enumerate(chunks) if b'"type":"response.completed"' in chunk
    )
    assert chunks[terminal_index + 1] == b"data: [DONE]\n\n"
    completion_calls = [
        args
        for operation, args in database.operations
        if operation == "fetchrow" and args and args[0] == call.call_id
    ]
    assert completion_calls == [(call.call_id, 11, 3, 17, "resp_1")]


@pytest.mark.asyncio
async def test_native_responses_rejects_unsupported_tool_schema_before_provider_http() -> None:
    captured = {"http_calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["http_calls"] += 1
        return httpx.Response(500, request=request)

    database = _Database()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = AgentModelPlane(
        database=database,
        provider_service=_ProviderService(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        http_client=client,
    )
    try:
        with pytest.raises(AgentModelPlaneError) as exc_info:
            _ = [
                chunk
                async for chunk in plane.stream(
                    body={
                        "model": "qwen3.7-plus",
                        "input": [{"role": "user", "content": "use a tool"}],
                        "tools": [{"type": "custom", "name": "apply_patch"}],
                    },
                    turn_metadata={},
                    authorized_call=_call(_profile()),
                )
            ]
    finally:
        await client.aclose()

    assert exc_info.value.code == "RUNTIME_TOOL_SCHEMA_UNSUPPORTED"
    assert captured["http_calls"] == 0
