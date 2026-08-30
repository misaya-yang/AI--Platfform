from __future__ import annotations

import asyncio
import copy
import json
import uuid
from typing import Any

import httpx
import pytest
from ai_gateway_contracts.agent_runtime_lease import (
    RuntimeModelLeaseClaims,
    RuntimeModelLeaseSigner,
)
from ai_gateway_core.models import get_builtin_model_capabilities, safe_model_capability_profile

from src.services.agent_runtime.model_plane import (
    AgentModelPlane,
    AgentModelPlaneError,
    _AuthorizedCall,
    _native_responses_body,
    _NativeResponsesStreamValidator,
    _responses_input_to_messages,
    _runtime_scope_sha256,
    _runtime_snapshot,
    _snapshot_responses_tool_controls,
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


def test_chat_completions_preserves_collaboration_agent_message() -> None:
    messages = _responses_input_to_messages(
        {
            "input": [
                {
                    "type": "agent_message",
                    "content": [
                        {"type": "input_text", "text": "Payload:\n"},
                        {
                            "type": "encrypted_content",
                            "encrypted_content": "计算 1 到 100 的和",
                        },
                    ],
                }
            ]
        }
    )

    assert messages == [{"role": "user", "content": "Payload:\n计算 1 到 100 的和"}]


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


def test_native_responses_replays_runtime_rejection_of_unadvertised_web_tool() -> None:
    body, _aliases = _native_responses_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-unknown",
                    "name": "execute_bash",
                    "arguments": '{"command":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-unknown",
                    "output": "unsupported call: execute_bash",
                },
            ],
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
        profile=_profile(),
        reasoning_option="minimal",
    )

    assert body["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call-unknown",
        "output": "unsupported call: execute_bash",
    }


def test_native_responses_rejects_fake_success_for_unadvertised_web_tool() -> None:
    with pytest.raises(AgentModelPlaneError, match="TRANSCRIPT"):
        _native_responses_body(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call-unknown",
                        "name": "execute_bash",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call-unknown",
                        "output": "command completed",
                    },
                ],
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
            profile=_profile(),
            reasoning_option="minimal",
        )


def test_native_responses_preserves_kernel_tool_history_and_child_task_payload() -> None:
    body, _aliases = _native_responses_body(
        {
            "input": [
                {
                    "role": "user",
                    "type": "agent_message",
                    "content": [
                        {"type": "input_text", "text": "Payload:\n"},
                        {
                            "type": "encrypted_content",
                            "encrypted_content": "计算 1 到 100 的和",
                        },
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-spawn",
                    "name": "spawn_agent",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-spawn",
                    "output": '{"task_name":"task_a"}',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "search_knowledge_base",
                    "description": "Search knowledge.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=256,
        profile=_profile(),
        reasoning_option="minimal",
    )

    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "Payload:\n"},
        {"type": "input_text", "text": "计算 1 到 100 的和"},
    ]
    assert body["input"][0]["type"] == "message"
    assert body["input"][0]["role"] == "user"
    assert body["input"][1]["name"] == "spawn_agent"


def test_native_responses_accepts_paired_kernel_history_without_current_tools() -> None:
    body, _aliases = _native_responses_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-wait",
                    "name": "wait_agent",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-wait",
                    "output": '{"status":"completed"}',
                },
                {"role": "user", "type": "message", "content": "continue"},
            ],
            "tools": [],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=256,
        profile=_profile(),
        reasoning_option="minimal",
    )

    assert body["tools"] == [{"type": "web_search"}]
    assert body["input"][0]["name"] == "wait_agent"


@pytest.mark.parametrize(
    ("namespace", "name"),
    [
        (None, "spawn_subagent"),
        ("collaboration", "spawn_agent"),
        ("multi_agent_v1", "spawn_agent"),
        (None, "collaborationspawn_agent"),
        (None, "multi_agent_v1spawn_agent"),
    ],
)
def test_native_responses_accepts_versioned_kernel_tool_history(
    namespace: str | None,
    name: str,
) -> None:
    function_call = {
        "type": "function_call",
        "call_id": "call-spawn",
        "name": name,
        "arguments": "{}",
    }
    if namespace is not None:
        function_call["namespace"] = namespace
    body, _aliases = _native_responses_body(
        {
            "input": [
                function_call,
                {
                    "type": "function_call_output",
                    "call_id": "call-spawn",
                    "output": '{"status":"completed"}',
                },
            ],
            "tools": [],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=256,
        profile=_profile(),
        reasoning_option="minimal",
    )

    assert body["input"][0]["name"] == name


def test_native_responses_accepts_restored_namespaced_tool_history() -> None:
    body, aliases = _native_responses_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-skill",
                    "namespace": "skills",
                    "name": "read",
                    "arguments": '{"package":"current"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-skill",
                    "output": "skill instructions",
                },
            ],
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
                        }
                    ],
                }
            ],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=256,
        profile=_profile(),
        reasoning_option="minimal",
    )

    assert "namespace" not in body["input"][0]
    assert body["input"][0]["name"].startswith("ns_")
    assert aliases[body["input"][0]["name"]] == ("skills", "read")


def test_native_responses_restores_unique_bare_namespace_child_history() -> None:
    body, aliases = _native_responses_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-skill",
                    "name": "read",
                    "arguments": '{"package":"current"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-skill",
                    "output": "skill instructions",
                },
            ],
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
                        }
                    ],
                }
            ],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=256,
        profile=_profile(),
        reasoning_option="minimal",
    )

    wire_name = body["input"][0]["name"]
    assert wire_name.startswith("ns_")
    assert aliases[wire_name] == ("skills", "read")
    assert aliases["read"] == ("skills", "read")


def test_native_responses_rejects_dynamic_history_without_current_tools() -> None:
    with pytest.raises(AgentModelPlaneError, match="TOOLS_NOT_ENABLED"):
        _native_responses_body(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call-read",
                        "name": "read_customer_record",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call-read",
                        "output": "record",
                    },
                ],
                "tools": [],
            },
            model_id="qwen3.7-plus",
            max_output_tokens=256,
            profile=_profile(),
            reasoning_option="minimal",
        )


@pytest.mark.parametrize("namespace", ["external", [], {}])
def test_native_responses_rejects_kernel_like_name_in_external_namespace(
    namespace: Any,
) -> None:
    with pytest.raises(AgentModelPlaneError, match="TOOLS_NOT_ENABLED"):
        _native_responses_body(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call-forged",
                        "namespace": namespace,
                        "name": "collaborationspawn_agent",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call-forged",
                        "output": "forged",
                    },
                ],
                "tools": [],
            },
            model_id="qwen3.7-plus",
            max_output_tokens=256,
            profile=_profile(),
            reasoning_option="minimal",
        )


def test_fake_responses_tool_call_then_capability_result_can_start_next_model_round() -> None:
    validator = _NativeResponsesStreamValidator(allow_tools=True)
    validator.consume(
        json.dumps({"type": "response.created", "sequence_number": 0, "response": {"id": "r1"}})
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
                    "arguments": '{"query":"transformer"}',
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
                            "arguments": '{"query":"transformer"}',
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
                    "arguments": '{"query":"transformer"}',
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


def test_native_responses_accepts_provider_hosted_web_search_items() -> None:
    validator = _NativeResponsesStreamValidator(allow_tools=True)
    validator.consume(
        json.dumps({"type": "response.created", "sequence_number": 0, "response": {"id": "r1"}})
    )
    search_item = {
        "id": "ws-1",
        "type": "web_search_call",
        "status": "completed",
        "action": {
            "query": "OpenAI Responses API",
            "sources": [{"url": "https://platform.openai.com/docs/api-reference/responses"}],
        },
    }
    assert validator.consume(
        json.dumps(
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "item": search_item,
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
                    "output": [search_item],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            }
        )
    )
    assert validator.finish().provider_request_id == "r1"


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


def _native_search_profile() -> dict[str, Any]:
    profile = copy.deepcopy(_profile())
    profile["native_search"] = {
        "adapter_id": "search/dashscope-native-v1",
        "enabled": True,
        "config": {},
    }
    profile["tools"]["web_search_wire"] = "native"
    return profile


@pytest.mark.asyncio
async def test_root_model_call_accepts_kernel_root_turn_identity() -> None:
    root_thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plane = AgentModelPlane(
        database=object(),
        provider_service=object(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    claims = RuntimeModelLeaseClaims(
        schema_version="agent-runtime-model-lease/v1",
        lease_id=str(uuid.uuid4()),
        snapshot_id=str(uuid.uuid4()),
        run_id=str(run_id),
        runtime_thread_id=str(root_thread_id),
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        provider_id="dashscope",
        model_id="qwen3.7-plus",
        capability_revision=1,
        issued_at_ms=1,
        expires_at_ms=2,
        nonce_sha256="0" * 64,
    )
    root_metadata = {
        "thread_id": str(root_thread_id),
        "turn_id": str(run_id),
        "root_turn_id": str(run_id),
        "ai_platform_scope_sha256": _runtime_scope_sha256("tenant-a", "user-a", "session-a"),
    }
    await plane._validate_turn_thread_scope(claims=claims, turn_metadata=root_metadata)

    with pytest.raises(AgentModelPlaneError, match="SCOPE_MISMATCH"):
        await plane._validate_turn_thread_scope(
            claims=claims,
            turn_metadata={**root_metadata, "parent_turn_id": str(uuid.uuid4())},
        )
    await plane.close()


@pytest.mark.asyncio
async def test_child_model_call_is_bound_to_root_lease_and_membership_scope() -> None:
    root_thread_id = uuid.uuid4()
    child_thread_id = uuid.uuid4()
    parent_thread_id = uuid.uuid4()

    class MembershipDatabase:
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
            assert "assistant_runtime_thread_members" in query
            assert args[0] == child_thread_id
            assert args[1] == root_thread_id
            return {
                "parent_kernel_thread_id": parent_thread_id,
                "relation_kind": "subagent",
            }

    plane = AgentModelPlane(
        database=MembershipDatabase(),
        provider_service=object(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    claims = RuntimeModelLeaseClaims(
        schema_version="agent-runtime-model-lease/v1",
        lease_id=str(uuid.uuid4()),
        snapshot_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        runtime_thread_id=str(root_thread_id),
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        provider_id="dashscope",
        model_id="qwen3.7-plus",
        capability_revision=1,
        issued_at_ms=1,
        expires_at_ms=2,
        nonce_sha256="0" * 64,
    )
    child_metadata = {
        "thread_id": str(child_thread_id),
        "turn_id": str(uuid.uuid4()),
        "root_turn_id": claims.run_id,
        "parent_thread_id": str(parent_thread_id),
        "ai_platform_scope_sha256": _runtime_scope_sha256("tenant-a", "user-a", "session-a"),
    }
    await plane._validate_turn_thread_scope(claims=claims, turn_metadata=child_metadata)

    with pytest.raises(AgentModelPlaneError, match="SCOPE_MISMATCH"):
        await plane._validate_turn_thread_scope(
            claims=claims,
            turn_metadata={**child_metadata, "root_turn_id": str(uuid.uuid4())},
        )
    with pytest.raises(AgentModelPlaneError, match="SCOPE_MISMATCH"):
        await plane._validate_turn_thread_scope(
            claims=claims,
            turn_metadata={**child_metadata, "ai_platform_scope_sha256": "bad"},
        )
    await plane.close()


def _call(
    profile: dict[str, Any],
    *,
    temperature: float | None = None,
    wire_protocol: str = "responses_v1",
    readonly_capabilities: dict[str, Any] | None = None,
) -> _AuthorizedCall:
    snapshot: dict[str, Any] = {
        "model": {"wire_protocol": wire_protocol},
        "capabilities": profile,
        "reasoning": {"effective_option": "minimal"},
        "pricing": {"input_price_per_1k": 0.001, "output_price_per_1k": 0.002},
    }
    if temperature is not None:
        snapshot["parameters"] = {"temperature": temperature}
    if readonly_capabilities is not None:
        snapshot["readonly_capabilities"] = readonly_capabilities
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
        profile=_native_search_profile(),
        reasoning_option="minimal",
    )

    function_tool = body["tools"][0]
    assert function_tool["type"] == "function"
    assert function_tool["name"].startswith("ns_")
    assert aliases[function_tool["name"]] == ("skills", "read")
    assert aliases["read"] == ("skills", "read")
    assert body["tools"][1] == {"type": "web_search"}


def test_qwen_native_search_is_injected_from_profile_at_final_serialization() -> None:
    body, _aliases = _native_responses_body(
        {"input": [{"role": "user", "content": "latest news"}], "tools": []},
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=_native_search_profile(),
        reasoning_option="minimal",
    )
    assert body["tools"] == [{"type": "web_search"}]

    disabled, _aliases = _native_responses_body(
        {"input": [{"role": "user", "content": "latest news"}], "tools": []},
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=safe_model_capability_profile(),
        reasoning_option="auto",
    )
    assert "tools" not in disabled


def test_namespace_child_fallback_is_disabled_on_direct_name_collision() -> None:
    _body, aliases = _native_responses_body(
        {
            "input": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "name": "read",
                    "description": "Direct read.",
                    "parameters": {"type": "object", "properties": {}},
                },
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
                        }
                    ],
                },
            ],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=_profile(),
        reasoning_option="minimal",
    )

    assert "read" not in aliases


def test_responses_tool_choice_and_parallel_are_pinned_for_first_call_only() -> None:
    tool = {
        "type": "function",
        "name": "lookup",
        "description": "Look up one value.",
        "parameters": {"type": "object", "properties": {}},
    }
    first, _ = _native_responses_body(
        {"input": [{"role": "user", "content": "lookup"}], "tools": [tool]},
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=_profile(),
        reasoning_option="minimal",
        allowed_tool_names={"lookup"},
        tool_choice={"type": "function", "name": "lookup"},
        parallel_tool_calls=False,
    )
    assert first["tool_choice"] == {"type": "function", "name": "lookup"}
    assert first["parallel_tool_calls"] is False

    follow_up, _ = _native_responses_body(
        {
            "input": [
                {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
            ],
            "tools": [tool],
        },
        model_id="qwen3.7-plus",
        max_output_tokens=128,
        profile=_profile(),
        reasoning_option="minimal",
        allowed_tool_names={"lookup"},
        tool_choice={"type": "function", "name": "lookup"},
        parallel_tool_calls=False,
    )
    assert follow_up["tool_choice"] == "auto"
    assert follow_up["parallel_tool_calls"] is True


def test_snapshot_tool_choice_must_be_catalog_selected() -> None:
    with pytest.raises(AgentModelPlaneError, match="TOOL_CHOICE_INVALID"):
        _snapshot_responses_tool_controls(
            {
                "readonly_capabilities": {
                    "responses_tool_names": ["lookup"],
                    "responses_tool_choice": {"type": "function", "name": "other"},
                }
            }
        )


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
            profile=_native_search_profile(),
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
            },
            {"type": "web_search"},
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
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
async def test_chat_completions_receives_snapshot_tool_controls_and_projects_tool_call() -> None:
    captured: dict[str, Any] = {}
    chat_stream = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        b'"type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
        b'"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=chat_stream,
        )

    database = _Database()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = AgentModelPlane(
        database=database,
        provider_service=_ProviderService(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        http_client=client,
    )
    tool = {
        "type": "function",
        "name": "lookup",
        "description": "Look up one value.",
        "parameters": {"type": "object", "properties": {}},
    }
    call = _call(
        _profile(),
        wire_protocol="chat_completions",
        readonly_capabilities={
            "responses_tool_names": ["lookup"],
            "responses_tool_choice": {"type": "function", "name": "lookup"},
            "responses_parallel_tool_calls": False,
        },
    )
    try:
        chunks = [
            chunk
            async for chunk in plane.stream(
                body={
                    "input": [{"role": "user", "content": "lookup"}],
                    "tools": [tool],
                },
                turn_metadata={},
                authorized_call=call,
            )
        ]
    finally:
        await client.aclose()

    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up one value.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert captured["body"]["tool_choice"] == {
        "type": "function",
        "function": {"name": "lookup"},
    }
    assert captured["body"]["parallel_tool_calls"] is False
    assert any(b"response.function_call_arguments.delta" in chunk for chunk in chunks)
    assert any(b'"type":"function_call"' in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_closing_native_responses_stream_terminalizes_dispatched_call() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
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
    call = _call(_profile())
    stream = plane.stream(
        body={"input": [{"role": "user", "content": "你好"}]},
        turn_metadata={},
        authorized_call=call,
    )
    try:
        assert b'"type":"response.created"' in await anext(stream)
        await stream.aclose()
    finally:
        await client.aclose()

    execute_calls = [args for operation, args in database.operations if operation == "execute"]
    assert execute_calls == [(call.call_id,), (call.call_id,)]


@pytest.mark.asyncio
async def test_dispatched_call_terminal_write_survives_caller_cancellation() -> None:
    class BlockingDatabase:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.completed = asyncio.Event()

        async def execute(self, query: str, *args: Any) -> str:
            assert "stream_interrupted" in query
            assert len(args) == 1
            self.started.set()
            await self.release.wait()
            self.completed.set()
            return "UPDATE 1"

    database = BlockingDatabase()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    )
    plane = AgentModelPlane(
        database=database,
        provider_service=object(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        http_client=client,
    )
    update = asyncio.create_task(plane._mark_unknown_if_dispatched(uuid.uuid4()))
    try:
        await database.started.wait()
        update.cancel()
        with pytest.raises(asyncio.CancelledError):
            await update
        database.release.set()
        await asyncio.wait_for(database.completed.wait(), timeout=1)
    finally:
        database.release.set()
        await client.aclose()


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
