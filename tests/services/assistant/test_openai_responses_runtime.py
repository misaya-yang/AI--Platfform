"""Internal OpenAI Responses Local Node runtime contracts.

These tests are offline protocol checks.  They never start a provider client,
Local Node transport, browser, desktop driver, or process.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator

import pytest
from ai_gateway_core.enums import ModelProvider
from ai_gateway_core.models import ChatMessage
from assistant_service.core.local_node import LocalNodeRunScope
from assistant_service.core.models.model_registry import ModelRegistry
from assistant_service.core.models.responses_api import (
    RESPONSES_V1_WIRE_PROTOCOL,
    build_responses_request,
    iter_responses_stream,
)
from assistant_service.core.providers import (
    OPENAI_LOCAL_PROVIDER_BLOCK,
    OpenAIResponsesComputerBinding,
    OpenAIResponsesLocalBindings,
    OpenAIResponsesRuntimeError,
    OpenAIResponsesShellBinding,
    prepare_openai_responses_local_runtime,
)

SCOPE = LocalNodeRunScope(
    tenant_id="tenant-offline",
    user_id="user-offline",
    session_id="session-offline",
    run_id="22222222-2222-4222-8222-222222222222",
)


class _Resolver:
    def __init__(self, bindings: OpenAIResponsesLocalBindings | None) -> None:
        self.bindings = bindings
        self.calls: list[tuple[LocalNodeRunScope, frozenset[str]]] = []

    async def resolve(
        self,
        scope: LocalNodeRunScope,
        *,
        required_tool_names: frozenset[str],
    ) -> OpenAIResponsesLocalBindings | None:
        self.calls.append((scope, required_tool_names))
        return self.bindings


class _ForbiddenNativeRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def tool_definitions(self) -> list[dict[str, object]]:
        self.calls += 1
        raise AssertionError("Qwen must not consume the OpenAI native adapter")


def _function_tool(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Offline standard function tool",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def _bindings(tmp_path) -> OpenAIResponsesLocalBindings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return OpenAIResponsesLocalBindings(
        scope=SCOPE,
        computer=OpenAIResponsesComputerBinding(
            app_grant_id="grant-app",
            window_id="window-1",
            observation_id="observation-1",
        ),
        shell=OpenAIResponsesShellBinding(
            grant_id="grant-workspace",
            workspace_root=str(workspace),
            cwd=".",
        ),
    )


async def _runtime(tmp_path):
    registry = ModelRegistry()
    registry.configure_provider(
        ModelProvider.OPENAI,
        "offline-configured-placeholder",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    resolver = _Resolver(_bindings(tmp_path))
    runtime, readiness = await prepare_openai_responses_local_runtime(
        scope=SCOPE,
        model_registry=registry,
        model_id="gpt-5.4",
        resolver=resolver,
        selected_tool_names=[
            "local_screen_observe",
            "local_app_control",
            "local_process_run",
        ],
    )
    assert readiness.to_dict() == {
        "status": "ready",
        "reason": "openai_responses_v1_configured",
    }
    assert runtime is not None
    return runtime


@pytest.mark.asyncio
async def test_missing_openai_key_is_exact_not_run_and_never_resolves_or_dispatches() -> None:
    registry = ModelRegistry()
    resolver = _Resolver(None)

    runtime, readiness = await prepare_openai_responses_local_runtime(
        scope=SCOPE,
        model_registry=registry,
        model_id="gpt-5.4",
        resolver=resolver,
        selected_tool_names=["local_screen_observe", "local_app_control"],
    )

    assert runtime is None
    assert readiness.to_dict() == {
        "status": "not_run",
        "reason": "openai_provider_not_configured",
    }
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_default_qwen_uses_only_standard_functions_and_never_consumes_adapter() -> None:
    registry = ModelRegistry()
    registry.configure_provider(
        ModelProvider.DASHSCOPE,
        "offline-configured-placeholder",
        wire_protocol=RESPONSES_V1_WIRE_PROTOCOL,
    )
    resolver = _Resolver(None)

    runtime, readiness = await prepare_openai_responses_local_runtime(
        scope=SCOPE,
        model_registry=registry,
        model_id="qwen3.7-plus",
        resolver=resolver,
        selected_tool_names=[
            "local_file_list",
            "local_file_search",
            "local_file_hash",
            "local_file_read",
            "local_file_watch",
        ],
    )

    assert runtime is None
    assert readiness.to_dict() == {
        "status": "not_run",
        "reason": "model_provider_is_not_openai",
    }
    assert resolver.calls == []

    forbidden = _ForbiddenNativeRuntime()
    body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="Inspect granted files")],
        tools=[
            _function_tool("local_file_list"),
            _function_tool("local_file_search"),
            _function_tool("local_file_hash"),
            _function_tool("local_file_read"),
            _function_tool("local_file_watch"),
        ],
        stream=True,
        openai_local_runtime=forbidden,
    )
    assert forbidden.calls == 0
    assert {item["type"] for item in body["tools"]} == {"function"}
    assert {item["name"] for item in body["tools"]} == {
        "local_file_list",
        "local_file_search",
        "local_file_hash",
        "local_file_read",
        "local_file_watch",
    }


@pytest.mark.asyncio
async def test_openai_runtime_declares_current_native_tools_and_projects_to_canonical_calls(
    tmp_path,
) -> None:
    runtime = await _runtime(tmp_path)
    assert runtime.tool_definitions() == [
        {"type": "computer"},
        {"type": "shell", "environment": {"type": "local"}},
    ]
    assert runtime.hidden_function_tool_names() == {
        "local_screen_observe",
        "local_app_control",
        "local_process_run",
    }

    projection = runtime.project_provider_item(
        {
            "id": "item-computer-1",
            "type": "computer_call",
            "call_id": "computer-call-1",
            "actions": [{"type": "click", "x": 12, "y": 34}],
            "pending_safety_checks": [
                {
                    "id": "check-1",
                    "code": "external_side_effect",
                    "message": "Confirm the exact target",
                }
            ],
            "status": "completed",
        }
    )
    assert projection.provider_block["type"] == OPENAI_LOCAL_PROVIDER_BLOCK
    assert len(projection.tool_calls) == 1
    call = projection.tool_calls[0]
    assert call["function"]["name"] == "local_app_control"
    arguments = json.loads(call["function"]["arguments"])
    assert arguments["app_grant_id"] == "grant-app"
    assert arguments["actions"] == [{"type": "click", "x": 12, "y": 34}]
    assert arguments["provider_safety_checks"][0]["id"] == "check-1"
    assert arguments["_middleware_approval_required"] is True


@pytest.mark.asyncio
async def test_provider_computer_action_never_silently_weakens_unsupported_semantics(
    tmp_path,
) -> None:
    runtime = await _runtime(tmp_path)
    with pytest.raises(OpenAIResponsesRuntimeError) as exc_info:
        runtime.project_provider_item(
            {
                "id": "item-computer-unsafe",
                "type": "computer_call",
                "call_id": "computer-call-unsafe",
                "actions": [{"type": "click", "button": "right", "x": 1, "y": 2}],
                "status": "completed",
            }
        )
    assert exc_info.value.code == "unsupported_local_computer_action"


@pytest.mark.asyncio
async def test_gateway_metadata_and_exact_safety_approval_gate_provider_continuation(
    tmp_path,
) -> None:
    runtime = await _runtime(tmp_path)
    projection = runtime.project_provider_item(
        {
            "id": "item-computer-2",
            "type": "computer_call",
            "call_id": "computer-call-2",
            "actions": [{"type": "screenshot"}],
            "pending_safety_checks": [
                {"id": "check-2", "code": "sensitive_data", "message": "Confirm capture"}
            ],
            "status": "completed",
        }
    )
    call = projection.tool_calls[0]
    image_url = "data:image/png;base64," + base64.b64encode(b"offline-fixture").decode()
    unapproved = runtime.result_block(
        provider_blocks=[projection.provider_block],
        call_id=call["id"],
        tool_name="local_screen_observe",
        success=True,
        result={"observation": {"image_url": image_url, "observation_id": "obs-after"}},
        error=None,
        metadata={"gateway_decision": {"allowed": True}, "queue_state": "awaiting_approval"},
    )
    assert unapproved is not None
    with pytest.raises(OpenAIResponsesRuntimeError) as exc_info:
        runtime.build_provider_output(projection.provider_block, [unapproved])
    assert exc_info.value.code == "canonical_local_execution_failed"

    approved = runtime.result_block(
        provider_blocks=[projection.provider_block],
        call_id=call["id"],
        tool_name="local_screen_observe",
        success=True,
        result={"observation": {"image_url": image_url, "observation_id": "obs-after"}},
        error=None,
        metadata={
            "gateway_decision": {"allowed": True},
            "queue_state": "succeeded",
            "approval_consumed": True,
        },
    )
    assert approved is not None
    output = runtime.build_provider_output(projection.provider_block, [approved])
    assert output["type"] == "computer_call_output"
    assert output["call_id"] == "computer-call-2"
    assert output["output"]["image_url"] == image_url
    assert output["acknowledged_safety_checks"] == [
        {"id": "check-2", "code": "sensitive_data", "message": "Confirm capture"}
    ]

    request = build_responses_request(
        model_id="gpt-5.4",
        messages=[
            ChatMessage(role="user", content="Observe the fixture"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=list(projection.tool_calls),
                provider_content_blocks=[projection.provider_block],
            ),
            ChatMessage(
                role="tool",
                content="server-owned local result",
                name="local_screen_observe",
                tool_call_id=call["id"],
                provider_content_blocks=[approved],
            ),
        ],
        temperature=0,
        max_output_tokens=256,
        tools=None,
        stream=True,
        local_runtime=runtime,
    )
    assert request["input"][-2] == projection.provider_block["provider_item"]
    assert request["input"][-1] == output


async def _lines(events: list[dict[str, object] | str]) -> AsyncIterator[str]:
    sequence = 0
    for event in events:
        if isinstance(event, str):
            yield f"data: {event}"
            continue
        value = dict(event)
        value["sequence_number"] = sequence
        sequence += 1
        yield "data: " + json.dumps(value)


@pytest.mark.asyncio
async def test_shell_call_stream_projects_offline_into_canonical_agentloop_tool_calls(
    tmp_path,
) -> None:
    runtime = await _runtime(tmp_path)
    initial = {
        "id": "item-shell-1",
        "type": "shell_call",
        "call_id": "shell-call-1",
        "action": {
            "commands": ["python -V", "git status --short"],
            "timeout_ms": 10_000,
            "max_output_length": 4_096,
        },
        "status": "in_progress",
    }
    completed = {**initial, "status": "completed"}
    response = {
        "id": "response-shell-1",
        "object": "response",
        "status": "completed",
        "output": [completed],
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    }
    deltas = [
        delta
        async for delta in iter_responses_stream(
            _lines(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "response-shell-1", "object": "response"},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": initial,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": completed,
                    },
                    {"type": "response.completed", "response": response},
                    "[DONE]",
                ]
            ),
            local_runtime=runtime,
        )
    ]
    projected = next(delta for delta in deltas if delta.tool_calls)
    assert [call["function"]["name"] for call in projected.tool_calls or []] == [
        "local_process_run",
        "local_process_run",
    ]
    assert projected.provider_content_blocks is not None
    assert projected.provider_content_blocks[0]["provider_call_id"] == "shell-call-1"
    assert deltas[-1].finish_reason == "tool_calls"
