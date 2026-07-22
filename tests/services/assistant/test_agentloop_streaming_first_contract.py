"""
Contract tests for AgentLoop Streaming-First mode.

Goal: ensure the AgentLoop streaming-first path emits the minimum set of events
required by the Assistant UI (Manus-style task/tool/artifact visualization).
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from assistant_service.core.agent.agent_loop import _redact_trace_text


@pytest.fixture(autouse=True)
def _isolate_task_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep repeated fixture session IDs isolated across contract cases."""

    from ai_gateway_core.tasks.task_manager import TaskManager
    from assistant_service.core.agent import agent_loop as agent_loop_module

    manager = TaskManager()
    monkeypatch.setattr(agent_loop_module, "get_task_manager", lambda: manager)


@dataclass
class MockUserContext:
    user_id: str
    tenant_id: str = "tenant1"
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = "127.0.0.1"
    roles: list[str] | None = None

    def __post_init__(self) -> None:
        if self.roles is None:
            self.roles = []


class FakeModelInfo:
    supports_vision = False
    context_window = 128000


class FakeModelRegistry:
    """A minimal ModelRegistry stub for AgentLoop.chat_stream."""

    def __init__(self, scripted: list[list[dict[str, Any]]]):
        """
        scripted: list of iterations; each iteration is a list of deltas.

        Each delta dict can include:
        - content: str
        - tool_calls: list[dict]
        - usage: dict
        """
        self._scripted = scripted
        self._call_index = 0
        self.last_messages: list[dict[str, Any]] | None = None
        self.messages_history: list[list[dict[str, Any]]] = []
        self.last_tools: list[dict[str, Any]] | None = None
        self.tools_history: list[list[dict[str, Any]] | None] = []
        self.native_search_history: list[dict[str, Any] | None] = []

    def get_model(self, _model_id: str) -> Any:
        return FakeModelInfo()

    async def chat_stream(self, *_args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        from assistant_service.core.models.model_registry import StreamDelta

        # Capture the prompt/messages passed by AgentLoop for assertions.
        self.last_messages = kwargs.get("messages")
        self.messages_history.append(copy.deepcopy(self.last_messages or []))
        self.last_tools = kwargs.get("tools")
        self.tools_history.append(self.last_tools)
        self.native_search_history.append(kwargs.get("native_search_config"))

        idx = self._call_index
        self._call_index += 1
        deltas = self._scripted[idx] if idx < len(self._scripted) else []
        for d in deltas:
            yield StreamDelta(
                content=d.get("content", ""),
                tool_calls=d.get("tool_calls"),
                usage=d.get("usage"),
                finish_reason=d.get("finish_reason"),
                provider_content_blocks=d.get("provider_content_blocks"),
            )


class FakeFailingModelRegistry(FakeModelRegistry):
    def __init__(self, error_message: str):
        super().__init__(scripted=[])
        self._error_message = error_message

    async def chat_stream(self, *_args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.last_messages = kwargs.get("messages")
        if False:
            yield None
        raise RuntimeError(self._error_message)


def _signed_agent_runtime_context():
    from assistant_service.core.agent.runtime_context import AgentRuntimeExecutionContext

    return AgentRuntimeExecutionContext(
        tenant_id="tenant1",
        caller_principal="u1",
        agent_id="11111111-1111-4111-8111-111111111111",
        agent_version_id="22222222-2222-4222-8222-222222222222",
        agent_draft_revision=None,
        publication_id=None,
        channel="preview",
        session_id="s1",
        runtime_fingerprint="sha256:runtime",
        agent_spec_hash="sha256:spec",
        prompt_hash="sha256:prompt",
        tool_schema_hash="sha256:tools",
        skills_hash="sha256:skills",
        knowledge_revision_hash="sha256:knowledge",
        memory_mode="session",
    )


class FakeToolDef:
    def __init__(self, name: str):
        self.name = name
        self.category = None
        self.description = "test tool"

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }


class FakeToolInvoker:
    def __init__(self, results_by_name: dict[str, dict[str, Any]]):
        self._results = results_by_name
        self.invocation_count = 0
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, context: Any, tool_names: list[str] | None = None) -> list[Any]:
        del context
        names = list(self._results.keys())
        if tool_names:
            names = [n for n in names if n in tool_names]
        return [FakeToolDef(n) for n in names]

    async def get_tool_definitions_filtered(
        self, context: Any, tool_names: list[str] | None = None
    ) -> list[Any]:
        return self.get_tool_definitions(context, tool_names)

    async def invoke(
        self, tool_name: str, arguments: dict[str, Any], context: Any, cancel_event: Any = None
    ) -> Any:
        from assistant_service.core.tools.tool_registry import ToolCallResult

        del context, cancel_event
        self.invocation_count += 1
        self.invocations.append((tool_name, arguments))
        payload = self._results.get(tool_name) or {}
        return ToolCallResult(
            call_id="internal",
            tool_name=tool_name,
            success=bool(payload.get("success", True)),
            result=payload.get("result", "ok"),
            error=payload.get("error"),
            duration_ms=float(payload.get("duration_ms", 12.3)),
            metadata=payload.get("metadata", {}) or {},
            output_files=payload.get("output_files", []) or [],
        )


class RecordingTraceWriter:
    write_timeout_s = 0.5

    def __init__(
        self,
        *,
        resume_sequence: int = 0,
        resume_error: Exception | None = None,
        strict_drain_error: Exception | None = None,
    ):
        self.persisted_resume_sequence = resume_sequence
        self.resume_error = resume_error
        self.strict_drain_error = strict_drain_error
        self.operations: list[str] = []
        self.started: list[Any] = []
        self.events: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.drain_timeouts: list[float] = []
        self.drain_strict: list[bool] = []
        self.drain_trace_ids: list[str | None] = []

    def start_trace(self, ctx: Any) -> bool:
        self.operations.append("start_trace")
        self.started.append(ctx)
        return True

    def record_event(self, **kwargs: Any) -> bool:
        self.operations.append("record_event")
        self.events.append(kwargs)
        return True

    def finish_trace(self, **kwargs: Any) -> bool:
        self.operations.append("finish_trace")
        self.finished.append(kwargs)
        return True

    async def drain(
        self,
        *,
        timeout_s: float = 1.0,
        strict: bool = False,
        trace_id: str | None = None,
    ) -> None:
        self.operations.append("drain")
        self.drain_timeouts.append(timeout_s)
        self.drain_strict.append(strict)
        self.drain_trace_ids.append(trace_id)
        if strict and self.strict_drain_error is not None:
            raise self.strict_drain_error

    async def resume_sequence(self, ctx: Any) -> int:
        del ctx
        self.operations.append("resume_sequence")
        if self.resume_error is not None:
            raise self.resume_error
        return self.persisted_resume_sequence


def _confirmation_loop(
    *,
    model: FakeModelRegistry,
    invoker: FakeToolInvoker,
    gateway: Any,
    trace_writer: RecordingTraceWriter | None,
) -> Any:
    from assistant_service.core.agent.agent_loop import AgentLoop
    from assistant_service.core.agent.middlewares.permission import (
        PermissionMiddleware,
        policy_from_sets,
    )

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=invoker,
        execution_gateway=gateway,
        trace_writer=trace_writer,  # type: ignore[arg-type]
    )
    loop.middleware_chain.add(PermissionMiddleware(policy_from_sets(confirm={"generate_image"})))
    return loop


async def _create_pending_approval(
    *,
    invoker: FakeToolInvoker,
    gateway: Any,
    trace_writer: RecordingTraceWriter,
    user: MockUserContext,
) -> tuple[str, str]:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    tool_calls = [
        {
            "id": "tc_approval",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        }
    ]
    loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=trace_writer,
    )
    events = []
    async for event in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
        history=[],
    ):
        events.append(event)
    run_id = next(event.data["run_id"] for event in events if event.event_type == "run_started")
    approval_id = next(
        event.data["approval_id"] for event in events if event.event_type == "approval_required"
    )
    return run_id, approval_id


def test_streaming_error_redaction_keeps_exception_type_and_redacts_provider_keys() -> None:
    class EmptyConnectError(Exception):
        def __str__(self) -> str:
            return ""

    assert "EmptyConnectError" in _redact_trace_text(EmptyConnectError())
    redacted = _redact_trace_text(
        "Incorrect API key provided: sk-fb4d4***********************f34c "
        "and google key AIzaSyA123456789012345678901234567890"
    )

    assert "sk-fb4d4" not in redacted
    assert "f34c" not in redacted
    assert "AIzaSyA" not in redacted
    assert "sk-[redacted]" in redacted
    assert "AIza[redacted]" in redacted


class FakeArtifact:
    def __init__(self, artifact_id: str):
        self.artifact_id = artifact_id


class FakeArtifactStorage:
    async def create_artifact(self, **_kwargs: Any) -> Any:
        return FakeArtifact("art_123")

    async def get_presigned_download_url(self, artifact: Any, expiry_seconds: int = 3600) -> str:
        del expiry_seconds
        return f"https://example.invalid/download/{artifact.artifact_id}"


def test_turn_kernel_rejects_illegal_transitions_and_duplicate_terminal() -> None:
    from assistant_service.core.turn_contract import (
        DuplicateTerminalError,
        TurnKernel,
        TurnState,
        TurnTransitionError,
    )

    kernel = TurnKernel(run_id="run-1", request_id="request-1")

    with pytest.raises(TurnTransitionError, match="created -> model_running"):
        kernel.transition(TurnState.MODEL_RUNNING)

    kernel.transition(TurnState.PREPARING, reason="accepted")
    terminal = kernel.finish(TurnState.SUCCEEDED, reason="complete")

    assert terminal["state"] == "succeeded"
    assert terminal["terminal"] is True
    assert [transition["to"] for transition in terminal["transitions"]] == [
        "preparing",
        "synthesizing",
        "succeeded",
    ]
    with pytest.raises(DuplicateTerminalError, match="already terminal"):
        kernel.finish(TurnState.FAILED)

    cancelled = TurnKernel(run_id="run-2", request_id="request-2")
    cancelled.transition(TurnState.PREPARING)
    cancelled.transition(TurnState.MODEL_RUNNING)
    assert cancelled.finish(TurnState.CANCELLED)["state"] == "cancelled"

    failed = TurnKernel(run_id="run-3", request_id="request-3")
    failed.transition(TurnState.PREPARING)
    assert failed.finish(TurnState.FAILED)["state"] == "failed"


def test_turn_kernel_attempt_identity_is_stable_and_resume_correlated() -> None:
    from assistant_service.core.turn_contract import TurnKernel

    first = TurnKernel(run_id="run-1", request_id="request-1")
    same = TurnKernel(run_id="run-1", request_id="request-1")
    resumed = TurnKernel(
        run_id="run-1",
        request_id="request-2",
        attempt_number=2,
        resumed_from_attempt_id=first.attempt_id,
    )

    assert first.attempt_id == same.attempt_id
    assert resumed.attempt_id != first.attempt_id
    assert resumed.snapshot()["resumed_from_attempt_id"] == first.attempt_id


@pytest.mark.parametrize(
    ("failure_class", "action", "retry_safety"),
    [
        ("transient_transport", "retry", "safe"),
        ("provider_refusal", "degrade", "unsafe"),
        ("approval_pending", "pause", "not_applicable"),
        ("resume_required", "resume", "not_applicable"),
        ("invalid_input", "abort", "unsafe"),
        ("tool_error", "abort", "needs_idempotency_or_read_back"),
        ("max_iterations", "abort", "unsafe"),
        ("cancelled", "abort", "not_applicable"),
        ("compensation_required", "compensate", "unsafe"),
    ],
)
def test_failure_class_maps_to_one_bounded_recovery_action(
    failure_class: str,
    action: str,
    retry_safety: str,
) -> None:
    from assistant_service.core.turn_contract import decide_failure

    decision = decide_failure(failure_class)

    assert decision.recovery_action.value == action
    assert decision.retry_safety.value == retry_safety


def test_unknown_side_effect_never_becomes_blind_retry() -> None:
    from assistant_service.core.turn_contract import decide_failure

    decision = decide_failure("tool_error", side_effect_state="unknown")

    assert decision.recovery_action.value == "pause"
    assert decision.retry_safety.value == "needs_idempotency_or_read_back"
    assert decision.side_effect_state.value == "unknown"


def test_synthesis_prompt_disables_all_transport_capability_claims() -> None:
    from assistant_service.core.agent.agent_loop import (
        AgentLoop,
        AgentLoopConfig,
        AgentLoopContext,
    )

    ctx = AgentLoopContext(
        session_id="s1",
        user_id="u1",
        tenant_id="tenant1",
        message="answer",
        config=AgentLoopConfig(
            model_id="test",
            kb_dataset_ids=["private-docs"],
            kb_mode="auto",
            web_search_enabled=True,
            os_agent_enabled=True,
            eval_system_prompt_override="EVAL_OVERRIDE_USE_SEARCH_KB_WEB_OS",
            agent_runtime=_signed_agent_runtime_context(),
            trusted_capability_instructions="CAPABILITY_OVERRIDE_USE_WRITE_DATA",
        ),
    )

    prompt, _candidate_hash = AgentLoop._build_streaming_system_prompt(
        ctx,
        available_tool_names=["write_data", "search_knowledge_base"],
        dataset_name_map={"private-docs": "Private docs"},
        capabilities_enabled=False,
    )

    assert "search_knowledge_base" not in prompt
    assert "write_data" not in prompt
    assert "Private docs" not in prompt
    assert "## Web Search" not in prompt
    assert "## Local OS Agent" not in prompt
    assert "EVAL_OVERRIDE_USE_SEARCH_KB_WEB_OS" not in prompt
    assert "CAPABILITY_OVERRIDE_USE_WRITE_DATA" not in prompt
    assert "has no tools, knowledge-base retrieval" in prompt


@pytest.mark.asyncio
async def test_streaming_first_emits_run_lifecycle_and_text() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    # One iteration, direct text, no tools.
    model = FakeModelRegistry(
        scripted=[
            [{"content": "Hello", "usage": {"input_tokens": 1, "output_tokens": 1}}],
        ]
    )
    loop = AgentLoop(model_registry=model)
    user = MockUserContext(user_id="u1")

    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=2)

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=cfg,
        history=[],
    ):
        events.append(ev.event_type)

    assert "run_started" in events
    assert "text_delta" in events
    assert "streaming_first_completed" in events
    assert "run_finished" in events


@pytest.mark.asyncio
async def test_streaming_first_replays_anthropic_pause_turn_blocks_verbatim() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    provider_blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "weather"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [],
        },
    ]
    native_config = {"tool_type": "web_search_20250305", "max_uses": 5}

    class PauseNativeRegistry(FakeModelRegistry):
        def get_model(self, _model_id: str) -> Any:
            return SimpleNamespace(
                supports_vision=False,
                context_window=128000,
                supports_native_search=True,
                native_search_config=native_config,
            )

    model = PauseNativeRegistry(
        scripted=[
            [
                {
                    "content": "Searching. ",
                    "finish_reason": "pause_turn",
                    "provider_content_blocks": provider_blocks,
                }
            ],
            [{"content": "Finished.", "finish_reason": "end_turn"}],
        ]
    )
    loop = AgentLoop(model_registry=model)

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="Search the web",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=3),
            history=[],
        )
    ]

    assert model._call_index == 2
    replay = model.messages_history[1][-1]
    assert replay["role"] == "assistant"
    assert replay["provider_content_blocks"] == provider_blocks
    assert replay["content"] == "Searching. "
    assert model.native_search_history == [native_config, native_config]
    assert all(event.event_type != "run_error" for event in events)
    assert any(event.event_type == "streaming_first_completed" for event in events)


@pytest.mark.asyncio
async def test_streaming_first_preserves_mixed_server_and_client_tool_blocks() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    native_config = {"tool_type": "web_search_20250305", "max_uses": 5}
    provider_blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "weather"},
        },
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "write_data",
            "input": {"value": 1},
        },
    ]

    class MixedNativeRegistry(FakeModelRegistry):
        def get_model(self, _model_id: str) -> Any:
            return SimpleNamespace(
                supports_vision=False,
                context_window=128000,
                supports_native_search=True,
                native_search_config=native_config,
            )

    model = MixedNativeRegistry(
        scripted=[
            [
                {
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "write_data",
                                "arguments": '{"value":1}',
                            },
                        }
                    ],
                    "finish_reason": "tool_use",
                    "provider_content_blocks": provider_blocks,
                }
            ],
            [{"content": "done", "finish_reason": "end_turn"}],
        ]
    )
    invoker = FakeToolInvoker({"write_data": {"success": True, "result": "written"}})
    loop = AgentLoop(model_registry=model, tool_invoker=invoker)  # type: ignore[arg-type]

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="Search the latest weather and write it",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=3),
            history=[],
        )
    ]

    assert invoker.invocations == [("write_data", {"value": 1})]
    assert model.native_search_history == [native_config, native_config]
    second_messages = model.messages_history[1]
    assistant = next(
        message
        for message in second_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["provider_content_blocks"] == provider_blocks
    tool_result = next(message for message in second_messages if message.get("role") == "tool")
    assert tool_result["tool_call_id"] == "toolu_1"
    assert all(event.event_type != "run_error" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason",
    [
        "length",
        "content_filter",
        "max_tokens",
        "model_context_window_exceeded",
        "safety",
    ],
)
async def test_streaming_first_never_marks_truncated_or_blocked_turn_successful(
    finish_reason: str,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(scripted=[[{"content": "partial", "finish_reason": finish_reason}]])
    loop = AgentLoop(model_registry=model)

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="answer",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
            history=[],
        )
    ]

    event_types = [event.event_type for event in events]
    assert "streaming_first_completed" not in event_types
    assert "error" in event_types
    finished = [event for event in events if event.event_type == "run_finished"]
    assert not finished or finished[-1].data["status"] != "succeeded"


@pytest.mark.asyncio
async def test_forced_synthesis_hides_incomplete_text_and_uses_no_capability_prompt() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    tool_calls = [
        {
            "id": "tc_write",
            "function": {"name": "write_data", "arguments": '{"value":1}'},
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls, "finish_reason": "tool_calls"}],
            [{"content": "unsafe partial", "finish_reason": "length"}],
            [{"content": "safe final", "finish_reason": "stop"}],
        ]
    )
    invoker = FakeToolInvoker({"write_data": {"success": True, "result": "written"}})
    loop = AgentLoop(model_registry=model, tool_invoker=invoker)  # type: ignore[arg-type]

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="Write the result",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=1,
                kb_dataset_ids=["private-docs"],
                kb_mode="tool",
                web_search_enabled=True,
                os_agent_enabled=True,
            ),
            history=[],
        )
    ]

    text = "".join(str(event.data) for event in events if event.event_type == "text_delta")
    assert "unsafe partial" not in text
    assert text.endswith("safe final")
    assert len(model.tools_history) == 3
    assert model.tools_history[1:] == [None, None]
    for messages in model.messages_history[1:]:
        prompt = str(messages[0].get("content") or "")
        assert "search_knowledge_base" not in prompt
        assert "## Web Search" not in prompt
        assert "## Local OS Agent" not in prompt
        assert "## Available Tools" not in prompt
    assert all(event.event_type != "run_error" for event in events)


@pytest.mark.asyncio
async def test_failed_forced_synthesis_never_emits_internal_success() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    tool_calls = [
        {
            "id": "tc_write",
            "function": {"name": "write_data", "arguments": '{"value":1}'},
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [
                {
                    "content": "working narrative",
                    "tool_calls": tool_calls,
                    "finish_reason": "tool_calls",
                }
            ],
            [{"content": "first unsafe", "finish_reason": "length"}],
            [{"content": "second unsafe", "finish_reason": "content_filter"}],
        ]
    )
    invoker = FakeToolInvoker({"write_data": {"success": True, "result": "written"}})
    loop = AgentLoop(model_registry=model, tool_invoker=invoker)  # type: ignore[arg-type]

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="Write the result",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        )
    ]

    text = "".join(str(event.data) for event in events if event.event_type == "text_delta")
    assert model._call_index == 3
    assert "first unsafe" not in text
    assert "second unsafe" not in text
    assert "working narrative" in text
    assert any(event.event_type == "run_error" for event in events)
    assert all(event.event_type != "streaming_first_completed" for event in events)
    assert all(event.event_type != "run_finished" for event in events)


@pytest.mark.asyncio
async def test_forced_synthesis_overflow_then_compact_success_has_one_success_terminal() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.runtime.context import ContextPacketOverflowError

    tool_calls = [
        {
            "id": "tc_write",
            "function": {"name": "write_data", "arguments": '{"value":1}'},
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls, "finish_reason": "tool_calls"}],
            [{"content": "compact answer", "finish_reason": "stop"}],
        ]
    )
    invoker = FakeToolInvoker({"write_data": {"success": True, "result": "written"}})
    loop = AgentLoop(model_registry=model, tool_invoker=invoker)  # type: ignore[arg-type]
    compile_attempts = 0

    def compile_with_first_overflow(
        _ctx: Any,
        *,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], None]:
        nonlocal compile_attempts
        compile_attempts += 1
        if compile_attempts == 1:
            raise ContextPacketOverflowError(
                model_context_window=128,
                overflow_tokens=17,
            )
        return copy.deepcopy(messages), None

    loop._compile_auxiliary_context_packet = compile_with_first_overflow  # type: ignore[method-assign]
    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="Write the result",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        )
    ]

    assert compile_attempts == 2
    assert model._call_index == 2
    assert all(event.event_type != "run_error" for event in events)
    assert sum(event.event_type == "streaming_first_completed" for event in events) == 1
    assert sum(event.event_type == "run_finished" for event in events) == 1
    overflow = next(
        event.data
        for event in events
        if event.event_type == "context_budget"
        and isinstance(event.data, dict)
        and event.data.get("status") == "overflow"
    )
    assert overflow["recoverable"] is True
    assert overflow["attempt"] == "full"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_enabled"),
    [("Write a poem", False), ("Search the latest weather", True)],
)
async def test_native_search_is_intent_gated(
    message: str,
    expected_enabled: bool,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    config = {"tool_type": "web_search_20250305", "max_uses": 5}

    class NativeSearchRegistry(FakeModelRegistry):
        def get_model(self, _model_id: str) -> Any:
            return SimpleNamespace(
                supports_vision=False,
                context_window=128000,
                supports_native_search=True,
                native_search_config=config,
            )

    model = NativeSearchRegistry(scripted=[[{"content": "done", "finish_reason": "end_turn"}]])
    loop = AgentLoop(model_registry=model)
    _ = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message=message,
            config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
            history=[],
        )
    ]

    assert model.native_search_history == [config if expected_enabled else None]


@pytest.mark.asyncio
async def test_agent_mixed_knowledge_auto_retrieves_and_tool_mode_stays_model_driven() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.tool_invoker import CapabilityAllowlist

    class Knowledge:
        def __init__(self) -> None:
            self.retrieve_calls: list[dict[str, Any]] = []

        async def list_datasets(self, _user: Any) -> list[dict[str, Any]]:
            return [
                {
                    "dataset_id": dataset_id,
                    "name": dataset_id,
                    "revision_fingerprint": "sha256:" + fingerprint * 64,
                }
                for dataset_id, fingerprint in (
                    ("dataset-auto", "1"),
                    ("dataset-tool", "2"),
                )
            ]

        async def retrieve(self, **kwargs: Any):
            self.retrieve_calls.append(dict(kwargs))
            dataset_id = str(kwargs["dataset_id"])
            return [
                SimpleNamespace(
                    metadata={},
                    image_url=None,
                    text=f"evidence-{dataset_id}",
                    score=0.91,
                    segment_id=f"segment-{dataset_id}",
                    document_id=f"document-{dataset_id}",
                )
            ], {"dataset_name": dataset_id}

    tool_calls = [
        {
            "id": "kb_tool_mode",
            "function": {
                "name": "search_knowledge_base",
                "arguments": '{"query":"tool evidence","dataset_ids":["dataset-tool"]}',
            },
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls}],
            [{"content": "answer from exact evidence"}],
        ]
    )
    tool_invoker = FakeToolInvoker(
        {
            "search_knowledge_base": {
                "success": True,
                "result": "tool-mode-evidence",
                "metadata": {"contexts": []},
            }
        }
    )
    knowledge = Knowledge()
    loop = AgentLoop(
        model_registry=model,
        kb_service=knowledge,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )
    config = AgentLoopConfig(
        model_id="test",
        max_tool_iterations=3,
        kb_dataset_ids=["dataset-auto", "dataset-tool"],
        kb_mode="auto",
        kb_retrieval_configs={
            "dataset-auto": {
                "mode": "auto",
                "top_k": 4,
                "threshold": 0.2,
                "include_images": False,
            },
            "dataset-tool": {
                "mode": "tool",
                "top_k": 9,
                "threshold": 0.8,
                "include_images": True,
            },
        },
        capability_allowlist=CapabilityAllowlist({"search_knowledge_base"}),
        agent_runtime=_signed_agent_runtime_context(),
    )

    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="Use our exact internal evidence",
        config=config,
        history=[],
    ):
        events.append(event)

    assert len(knowledge.retrieve_calls) == 1
    assert knowledge.retrieve_calls[0]["dataset_id"] == "dataset-auto"
    assert knowledge.retrieve_calls[0]["top_k"] == 4
    assert knowledge.retrieve_calls[0]["score_threshold"] == 0.2
    assert knowledge.retrieve_calls[0]["include_images"] is False
    assert tool_invoker.invocations == [
        (
            "search_knowledge_base",
            {"query": "tool evidence", "dataset_ids": ["dataset-tool"]},
        )
    ]
    assert model.tools_history[0] is not None
    assert {tool["function"]["name"] for tool in model.tools_history[0] or []} == {
        "search_knowledge_base"
    }
    first_user_message = next(
        message
        for message in model.last_messages or []
        if message.get("role") == "user"
        and "Use our exact internal evidence" in message.get("content", "")
    )
    assert "evidence-dataset-auto" in first_user_message["content"]
    assert "dataset-tool" not in str(knowledge.retrieve_calls)
    assert "dataset-off" not in str(knowledge.retrieve_calls)
    assert any(event.event_type == "context_retrieved" for event in events)
    assert any(event.event_type == "run_finished" for event in events)


@pytest.mark.asyncio
async def test_agent_auto_knowledge_failure_stops_before_model_fallback() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    class FailingKnowledge:
        def __init__(self) -> None:
            self.retrieve_calls = 0

        async def list_datasets(self, _user: Any) -> list[dict[str, Any]]:
            return [
                {
                    "dataset_id": "dataset-auto",
                    "name": "dataset-auto",
                    "revision_fingerprint": "sha256:" + "1" * 64,
                }
            ]

        async def retrieve(self, **_kwargs: Any):
            self.retrieve_calls += 1
            raise RuntimeError("retrieval unavailable")

    knowledge = FailingKnowledge()
    model = FakeModelRegistry(scripted=[[{"content": "must not run"}]])
    loop = AgentLoop(model_registry=model, kb_service=knowledge)
    config = AgentLoopConfig(
        model_id="test",
        max_tool_iterations=1,
        kb_dataset_ids=["dataset-auto"],
        kb_mode="auto",
        kb_retrieval_configs={
            "dataset-auto": {
                "mode": "auto",
                "top_k": 4,
                "threshold": 0.2,
                "include_images": False,
            }
        },
        agent_runtime=_signed_agent_runtime_context(),
    )

    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="Use the bound policy",
        config=config,
        history=[],
    ):
        events.append(event)

    assert knowledge.retrieve_calls == 1
    assert model._call_index == 0
    error = next(event for event in events if event.event_type == "run_error")
    assert error.data["error"] == "AGENT_KNOWLEDGE_UNAVAILABLE"
    assert all(event.event_type != "run_finished" for event in events)


@pytest.mark.asyncio
async def test_exact_agent_skill_is_first_turn_visible_invokable_and_not_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ai_gateway_core.skills import SkillManifest, SkillRegistry, SkillSource
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.skills.tool_bridge import skill_tool_name
    from assistant_service.core.tool_invoker import (
        CapabilityAllowlist,
        RegistryToolInvoker,
    )
    from assistant_service.core.tools.tool_registry import ToolRegistry

    skill_id = "11111111-1111-4111-8111-111111111111"
    version_id = "21111111-1111-4111-8111-111111111111"
    manifest = SkillManifest(
        name="report-helper",
        title="Report Helper",
        description="Create a report",
        summary="Create a report",
        entrypoint=f"db://{skill_id}/{version_id}",
        instructions="EXACT FIRST-TURN INSTRUCTIONS",
        permissions=["knowledge:read"],
        source=SkillSource.USER,
        artifact_type="tenant_instruction",
        skill_id=skill_id,
        version_id=version_id,
        content_hash="a" * 64,
        enabled=True,
    )

    class Repository:
        def __init__(self, _database: Any):
            pass

        async def load_versions(self, **values: Any) -> list[SkillManifest]:
            assert values["tenant_id"] == "tenant1"
            assert values["user_id"] == "u1"
            assert values["version_ids"] == frozenset({version_id})
            return [manifest]

    import ai_gateway_core.skills.registry as registry_module

    monkeypatch.setattr(registry_module, "DatabaseSkillArtifactRepository", Repository)
    tool_name = skill_tool_name("report-helper", version_id)
    model = FakeModelRegistry(
        scripted=[
            [
                {
                    "tool_calls": [
                        {
                            "id": "skill-call-1",
                            "function": {
                                "name": tool_name,
                                "arguments": '{"input":"quarterly report"}',
                            },
                        }
                    ]
                }
            ],
            [{"content": "done"}],
        ]
    )
    global_tools = ToolRegistry()
    invoker = RegistryToolInvoker(global_tools)
    loop = AgentLoop(model_registry=model, tool_invoker=invoker)
    loop.assistant_runtime = SimpleNamespace(
        features=SimpleNamespace(skills=True, memory_v2=False, context_v2=False),
        skill_registry=SkillRegistry(database=object()),
    )
    user = MockUserContext(user_id="u1", roles=["admin"])
    config = AgentLoopConfig(
        model_id="test",
        max_tool_iterations=2,
        skills_enabled=True,
        allowed_skill_ids=frozenset({"report-helper"}),
        allowed_skill_versions={"report-helper": version_id},
        capability_allowlist=CapabilityAllowlist(frozenset({tool_name})),
    )

    events = []
    async for event in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="create a report",
        config=config,
        history=[],
    ):
        events.append(event)

    first_turn_names = {schema["function"]["name"] for schema in (model.tools_history[0] or [])}
    completed = [event.data for event in events if event.event_type == "tool_call_completed"]
    assert tool_name in first_turn_names
    assert any(event.event_type == "skill_loaded" for event in events)
    assert completed[0]["success"] is True
    assert "EXACT FIRST-TURN INSTRUCTIONS" in completed[0]["result_preview"]
    assert global_tools.get_tool(tool_name) is None


@pytest.mark.asyncio
async def test_exact_agent_skill_kill_switch_fails_before_model_execution() -> None:
    from types import SimpleNamespace

    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.skills.tool_bridge import skill_tool_name
    from assistant_service.core.tool_invoker import CapabilityAllowlist

    version_id = "21111111-1111-4111-8111-111111111111"
    tool_name = skill_tool_name("report-helper", version_id)
    model = FakeModelRegistry(scripted=[[{"content": "must not execute"}]])
    loop = AgentLoop(model_registry=model)
    loop.assistant_runtime = SimpleNamespace(
        features=SimpleNamespace(skills=True, memory_v2=False, context_v2=False),
        skill_registry=object(),
    )
    config = AgentLoopConfig(
        model_id="test",
        skills_enabled=False,
        allowed_skill_ids=frozenset({"report-helper"}),
        allowed_skill_versions={"report-helper": version_id},
        capability_allowlist=CapabilityAllowlist(frozenset({tool_name})),
    )

    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="create a report",
        config=config,
        history=[],
    ):
        events.append(event)

    errors = [event.data for event in events if event.event_type == "run_error"]
    assert errors[0]["error"] == "AGENT_SKILL_UNAVAILABLE"
    assert model.tools_history == []


@pytest.mark.asyncio
async def test_revoked_exact_agent_skill_fails_before_model_execution() -> None:
    from types import SimpleNamespace

    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.skills.tool_bridge import skill_tool_name
    from assistant_service.core.tool_invoker import CapabilityAllowlist

    version_id = "21111111-1111-4111-8111-111111111111"
    tool_name = skill_tool_name("report-helper", version_id)

    class RevokedRegistry:
        def fork_runtime_view(self) -> RevokedRegistry:
            return self

        async def load_versions_from_database(self, **_values: Any) -> int:
            raise RuntimeError("revoked artifact must stay unavailable")

    model = FakeModelRegistry(scripted=[[{"content": "must not execute"}]])
    loop = AgentLoop(model_registry=model)
    loop.assistant_runtime = SimpleNamespace(
        features=SimpleNamespace(skills=True, memory_v2=False, context_v2=False),
        skill_registry=RevokedRegistry(),
    )
    config = AgentLoopConfig(
        model_id="test",
        skills_enabled=True,
        allowed_skill_ids=frozenset({"report-helper"}),
        allowed_skill_versions={"report-helper": version_id},
        capability_allowlist=CapabilityAllowlist(frozenset({tool_name})),
    )

    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="create a report",
        config=config,
        history=[],
    ):
        events.append(event)

    errors = [event.data for event in events if event.event_type == "run_error"]
    assert errors[0]["error"] == "AGENT_SKILL_UNAVAILABLE"
    assert model.tools_history == []


@pytest.mark.asyncio
async def test_streaming_first_emits_turn_contract_snapshot_and_terminal_envelope() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(
        scripted=[
            [{"content": "Hello", "usage": {"input_tokens": 1, "output_tokens": 1}}],
        ]
    )
    loop = AgentLoop(model_registry=model)
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi with Authorization: Bearer super-secret-value",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
        history=[],
    ):
        events.append(ev)

    run_started = next(ev.data for ev in events if ev.event_type == "run_started")
    completed = next(ev.data for ev in events if ev.event_type == "streaming_first_completed")
    run_finished = next(ev.data for ev in events if ev.event_type == "run_finished")

    assert run_started["context_snapshot"]["schema_version"] == "assistant-turn-contract/v1"
    assert run_started["context_snapshot"]["snapshot_id"].startswith("ctx_")
    assert run_started["attempt_number"] == 1
    assert run_started["attempt_id"].startswith("att_")
    assert run_started["context_snapshot"]["attempt_id"] == run_started["attempt_id"]
    assert completed["terminal_envelope"]["exit_reason"] == "succeeded"
    assert completed["terminal_envelope"]["context_snapshot_id"].startswith("ctx_")
    assert run_finished["terminal_envelope"]["status"] == "succeeded"
    assert run_finished["terminal_envelope"]["thread_id"] == "s1"
    assert run_finished["attempt_id"] == run_started["attempt_id"]
    assert run_finished["terminal_envelope"]["attempt_id"] == run_started["attempt_id"]
    assert run_finished["terminal_envelope"]["turn_state"]["state"] == "succeeded"
    assert run_finished["terminal_envelope"]["turn_state"]["terminal"] is True
    assert "failure_decision" not in run_finished["terminal_envelope"]
    assert sum(ev.event_type == "run_finished" for ev in events) == 1
    assert all(ev.event_type != "run_error" for ev in events)
    assert "super-secret-value" not in json.dumps(run_finished, default=str)


@pytest.mark.asyncio
async def test_streaming_first_persists_checkpoints_without_prompt_text() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    model = FakeModelRegistry(
        scripted=[
            [{"content": "Hello", "usage": {"input_tokens": 1, "output_tokens": 1}}],
        ]
    )
    tool_invoker = FakeToolInvoker({})
    gateway = AssistantExecutionGateway(tool_invoker=tool_invoker, database=None)
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        execution_gateway=gateway,
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=2)

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi with Authorization: Bearer raw-token",
        config=cfg,
        history=[],
    ):
        events.append(ev)

    run_id = next(ev.data["run_id"] for ev in events if ev.event_type == "run_started")
    checkpoints = gateway._checkpoints[run_id]  # AUDIT-OK: DB-less test fallback only
    phases = [checkpoint.phase for checkpoint in checkpoints]
    serialized = str([gateway._checkpoint_to_dict(checkpoint) for checkpoint in checkpoints])

    assert "run_started" in phases
    assert "model_turn_started" in phases
    assert "run_succeeded" in phases
    assert "raw-token" not in serialized
    assert all(checkpoint.message_state_hash for checkpoint in checkpoints)


@pytest.mark.asyncio
async def test_streaming_first_emits_context_budget_without_prompt_text() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(
        scripted=[
            [{"content": "Hello", "usage": {"input_tokens": 1, "output_tokens": 1}}],
        ]
    )
    loop = AgentLoop(model_registry=model)
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=2)

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi with auth header super-secret-value",
        config=cfg,
        history=[],
    ):
        events.append(ev)

    event_types = [ev.event_type for ev in events]
    assert "context_budget" in event_types
    assert event_types.index("context_budget") < event_types.index("text_delta")

    budget_payload = next(ev.data for ev in events if ev.event_type == "context_budget")
    assert budget_payload["run_id"]
    assert budget_payload["thread_id"] == "s1"
    assert budget_payload["mode"] == "streaming_first"
    assert budget_payload["message_count"] >= 2
    assert budget_payload["tool_count"] >= 0
    assert budget_payload["system_prompt_chars"] > 0
    assert len(budget_payload["prompt_prefix_hash"]) == 16
    assert len(budget_payload["system_prompt_hash"]) == 16
    assert budget_payload["prompt_prefix_message_count"] == 1
    assert budget_payload["prompt_prefix_chars"] == budget_payload["system_prompt_chars"]
    assert len(budget_payload["tool_schema_order_hash"]) == 16
    assert len(budget_payload["tool_schema_names_hash"]) == 16
    assert len(budget_payload["tool_schema_hash"]) == 16
    assert len(budget_payload["available_tool_schema_hash"]) == 16
    assert len(budget_payload["candidate_system_prompt_hash"]) == 16
    assert (
        budget_payload["context_snapshot"]["tools"]["available_tool_schema_hash"]
        == budget_payload["available_tool_schema_hash"]
    )
    assert len(budget_payload["runtime_revision"]) == 64
    assert budget_payload["context_snapshot"]["bootstrap"]["temperature"] == 0.5
    assert budget_payload["context_snapshot"]["policy"]["rag_config_hash"]
    assert budget_payload["context_snapshot"]["policy"]["rag_revision_hash"]
    assert budget_payload["context_estimated_input_tokens"] > 0
    assert budget_payload["context_window_tokens"] == 128000
    assert 0 <= budget_payload["context_utilization"] <= 1
    assert "super-secret-value" not in json.dumps(budget_payload, default=str)


@pytest.mark.asyncio
async def test_legacy_history_compaction_receipt_reaches_runtime_snapshot() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    class CompactingModelRegistry(FakeModelRegistry):
        async def chat(self, *_args: Any, **_kwargs: Any) -> tuple[str, dict[str, int]]:
            return "validated historical summary", {}

    model = CompactingModelRegistry(scripted=[[{"content": "done"}]])
    loop = AgentLoop(model_registry=model)
    history = [
        {"role": "user", "content": "HARD CONSTRAINT: never deploy"},
        {"role": "assistant", "content": "old verbose evidence " + "x" * 20_000},
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]
    original = copy.deepcopy(history)

    events = [
        event
        async for event in loop.execute(
            session_id="legacy-history-compaction",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="current request remains explicit",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=1,
                use_context_engine=False,
                max_history_tokens=1200,
                min_recent_messages=2,
                enable_history_trimming=True,
            ),
            history=history,
        )
    ]

    assert history == original
    assert model.last_messages is not None
    serialized_prompt = json.dumps(model.last_messages, ensure_ascii=False)
    assert "Historical generated summary (untrusted context" in serialized_prompt
    assert "HARD CONSTRAINT: never deploy" in serialized_prompt
    assert "recent question" in serialized_prompt
    assert "current request remains explicit" in serialized_prompt
    budget_payload = next(event.data for event in events if event.event_type == "context_budget")
    receipt = budget_payload["context_snapshot"]["bootstrap"]["history_compaction"]
    assert receipt["status"] == "committed"
    assert receipt["compacted"] is True
    assert receipt["compaction_lineage"]["reason"] == "history_preprocess"
    assert receipt["compaction_lineage"]["summary_provenance"]["untrusted"] is True
    assert all(event.event_type != "run_error" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_context_engine", [True, False])
async def test_streaming_context_packet_feature_switch(
    use_context_engine: bool,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(scripted=[[{"content": "done"}]])
    loop = AgentLoop(model_registry=model)
    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="feature-switch-query",
        config=AgentLoopConfig(
            model_id="test",
            max_tool_iterations=2,
            use_context_engine=use_context_engine,
        ),
        history=[],
    ):
        events.append(event)

    budget_events = [event for event in events if event.event_type == "context_budget"]
    if use_context_engine:
        assert [event.data["mode"] for event in budget_events] == [
            "streaming_first",
            "model_boundary",
        ]
        for event in budget_events:
            receipt = event.data["context_packet"]
            assert receipt["schema_version"] == "assistant-context-packet/v1"
            assert "feature-switch-query" not in json.dumps(receipt)
    else:
        assert [event.data["mode"] for event in budget_events] == ["streaming_first"]
        assert "context_packet" not in budget_events[0].data
    event_types = [event.event_type for event in events]
    assert "run_error" not in event_types
    assert "run_finished" in event_types


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_mode", "expect_memory"),
    [("auto", True), ("off", False)],
)
async def test_legacy_context_path_respects_memory_policy(
    memory_mode: str,
    expect_memory: bool,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    class LegacyMemoryService:
        def __init__(self) -> None:
            self.long_term_reads = 0

        async def get_session_memory(self, **_kwargs: Any) -> Any:
            return None

        async def set_session_memory(self, **_kwargs: Any) -> bool:
            return True

        async def get_long_term_context(self, **_kwargs: Any) -> dict[str, Any]:
            self.long_term_reads += 1
            return {"preferences": {"response_style": "legacy-long-term-marker"}}

        async def get_user_memory(self, **_kwargs: Any) -> Any:
            return None

        async def set_user_memory(self, **_kwargs: Any) -> bool:
            return True

    class LegacyRuntimeMemory:
        features = SimpleNamespace(skills=False, memory_v2=False, context_v2=False)

        def __init__(self) -> None:
            self.load_calls = 0

        async def load_memory_context(self, **_kwargs: Any) -> Any:
            self.load_calls += 1
            return SimpleNamespace(
                snippets=[
                    SimpleNamespace(
                        source_type="daily",
                        content="legacy-runtime-memory-marker",
                    )
                ],
                provenance=[{"source_type": "daily"}],
                loaded_sources=["daily"],
                fallback_used=False,
                fallback_reason=None,
            )

        async def schedule_daily_reflection(self, **_kwargs: Any) -> None:
            return None

    memory_service = LegacyMemoryService()
    runtime = LegacyRuntimeMemory()
    model = FakeModelRegistry(scripted=[[{"content": "done"}]])
    loop = AgentLoop(
        model_registry=model,
        memory_service=memory_service,  # type: ignore[arg-type]
        runtime_adapter=runtime,  # type: ignore[arg-type]
    )

    events = [
        event
        async for event in loop.execute(
            session_id=f"legacy-memory-{memory_mode}",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="legacy context compatibility query",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=1,
                use_context_engine=False,
                memory_mode=memory_mode,
                memory_profile="hybrid",
                runtime_mode="compat",
            ),
            history=[],
        )
    ]

    prompt = json.dumps(model.last_messages, ensure_ascii=False)
    if expect_memory:
        assert "legacy-long-term-marker" in prompt
        assert "legacy-runtime-memory-marker" in prompt
        assert memory_service.long_term_reads == 1
        assert runtime.load_calls == 1
    else:
        assert "legacy-long-term-marker" not in prompt
        assert "legacy-runtime-memory-marker" not in prompt
        assert memory_service.long_term_reads == 0
        assert runtime.load_calls == 0
    assert all(event.event_type != "run_error" for event in events)


@pytest.mark.asyncio
async def test_streaming_packet_survives_two_complete_tool_rounds() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(
        scripted=[
            [
                {
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"round":1}',
                            },
                        }
                    ]
                }
            ],
            [
                {
                    "tool_calls": [
                        {
                            "id": "tc2",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"round":2}',
                            },
                        }
                    ]
                }
            ],
            [{"content": "done"}],
        ]
    )
    invoker = FakeToolInvoker(
        {
            "lookup": {
                "success": True,
                "result": "private-tool-result-sentinel",
            }
        }
    )
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=invoker,  # type: ignore[arg-type]
    )
    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="run two rounds",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=4),
        history=[],
    ):
        events.append(event)

    assert invoker.invocations == [
        ("lookup", {"round": 1}),
        ("lookup", {"round": 2}),
    ]
    assert model.last_messages is not None
    assert [message["role"] for message in model.last_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assistant_calls = [
        message["tool_calls"][0]["id"]
        for message in model.last_messages
        if message["role"] == "assistant"
    ]
    tool_results = [
        message["tool_call_id"] for message in model.last_messages if message["role"] == "tool"
    ]
    assert assistant_calls == tool_results == ["tc1", "tc2"]
    assert len(model.tools_history) == 3
    assert all(
        [tool["function"]["name"] for tool in tools or []] == ["lookup"]
        for tools in model.tools_history
    )
    boundary_events = [
        event
        for event in events
        if event.event_type == "context_budget" and event.data.get("mode") == "model_boundary"
    ]
    assert len(boundary_events) == 3
    assert "private-tool-result-sentinel" not in json.dumps(
        [event.data["context_packet"] for event in boundary_events]
    )
    event_types = [event.event_type for event in events]
    assert "run_error" not in event_types
    assert "run_finished" in event_types


@pytest.mark.asyncio
async def test_streaming_rag_revision_hash_changes_with_dataset_catalog() -> None:
    from assistant_service.core.agent.agent_loop import (
        AgentLoop,
        AgentLoopConfig,
        AgentLoopContext,
    )

    class FakeKnowledgeService:
        rows = [
            {
                "dataset_id": "kb-a",
                "name": "Policies",
                "updated_at": "2026-07-14T00:00:00Z",
                "revision_fingerprint": "sha256:" + "1" * 64,
                "embedding_model": "text-embedding-v4",
                "statistics": {"document_count": 2, "segment_count": 10},
            }
        ]

        async def list_datasets(self, _user: Any) -> list[dict[str, Any]]:
            return self.rows

    knowledge = FakeKnowledgeService()
    loop = AgentLoop(
        model_registry=FakeModelRegistry(scripted=[]),
        kb_service=knowledge,  # type: ignore[arg-type]
    )
    user = MockUserContext(user_id="u1")
    context = AgentLoopContext(
        session_id="s1",
        user_id="u1",
        tenant_id="default",
        message="policy",
        config=AgentLoopConfig(model_id="test", kb_dataset_ids=["kb-a"]),
        user=user,  # type: ignore[arg-type]
    )

    names, first_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        user,  # type: ignore[arg-type]
    )
    knowledge.rows = [
        {
            **knowledge.rows[0],
            "revision_fingerprint": "sha256:" + "2" * 64,
        }
    ]
    _, second_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        user,  # type: ignore[arg-type]
    )

    assert names == {"kb-a": "Policies"}
    assert first_hash != second_hash


@pytest.mark.asyncio
async def test_streaming_first_usage_preserves_provider_cache_metrics() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(
        scripted=[
            [
                {
                    "content": "Hello",
                    "usage": {
                        "prompt_tokens": 100,
                        "prompt_tokens_details": {"cached_tokens": 44},
                        "completion_tokens": 12,
                    },
                }
            ],
        ]
    )
    loop = AgentLoop(model_registry=model)
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=2)

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=cfg,
        history=[],
    ):
        events.append(ev)

    run_finished = next(ev.data for ev in events if ev.event_type == "run_finished")
    usage = run_finished["metadata"]["usage"]

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 12
    assert usage["cached_input_tokens"] == 44


@pytest.mark.asyncio
async def test_streaming_first_system_prompt_keeps_client_prompt_out_of_system() -> None:
    """
    Regression: frontend may send a style-only system_prompt.
    Streaming-first must keep the base tool/KB instructions in the system prompt,
    but client-supplied prompt text must ride on the user turn with lower priority.
    """
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(
        scripted=[
            [{"content": "ok"}],
        ]
    )
    loop = AgentLoop(model_registry=model)
    user = MockUserContext(user_id="u1")

    cfg = AgentLoopConfig(
        model_id="test",
        kb_dataset_ids=["d1"],
        kb_mode="auto",
        system_prompt="STYLE_ONLY_PROMPT",
        max_tool_iterations=1,
    )

    async for _ in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=cfg,
        history=[],
    ):
        pass

    assert model.last_messages, "AgentLoop did not send messages to the model"
    assert model.last_messages[0]["role"] == "system"
    sys_content = str(model.last_messages[0].get("content") or "")
    assert "search_knowledge_base" in sys_content
    assert "STYLE_ONLY_PROMPT" not in sys_content
    assert "Additional System Instructions" not in sys_content

    user_content = str(model.last_messages[-1].get("content") or "")
    assert "User Custom Instructions" in user_content
    assert "STYLE_ONLY_PROMPT" in user_content


@pytest.mark.asyncio
async def test_streaming_first_uses_trusted_eval_prompt_as_system_message() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeModelRegistry(scripted=[[{"content": "ok"}]])
    loop = AgentLoop(model_registry=model)
    cfg = AgentLoopConfig(
        model_id="test",
        system_prompt="STYLE_ONLY_PROMPT",
        eval_system_prompt_override="TRUSTED_EVAL_PROMPT",
        max_tool_iterations=1,
    )

    async for _ in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="eval-candidate"),  # type: ignore[arg-type]
        message="Hi",
        config=cfg,
        history=[],
    ):
        pass

    assert model.last_messages[0] == {
        "role": "system",
        "content": "TRUSTED_EVAL_PROMPT",
    }
    assert "STYLE_ONLY_PROMPT" in str(model.last_messages[-1].get("content") or "")


@pytest.mark.asyncio
async def test_side_effect_unknown_hard_pauses_before_another_model_turn() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    tool_calls = [
        {
            "id": "tc_unknown_write",
            "function": {
                "name": "external_write",
                "arguments": '{"value":"same"}',
            },
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls}],
            [{"content": "must not continue"}],
        ]
    )
    invoker = FakeToolInvoker(
        results_by_name={
            "external_write": {
                "success": False,
                "error": "MCP_CANCELLED_AFTER_DISPATCH",
                "metadata": {
                    "side_effect_unknown": True,
                    "tool_operation": {
                        "operation_id": "tool-op-1",
                        "read_back_available": True,
                        "compensation_available": False,
                    },
                    "tool_failure": {
                        "failure_kind": "side_effect_unknown",
                        "side_effect_state": "unknown",
                        "recovery_action": "resume",
                    },
                },
            }
        }
    )
    loop = AgentLoop(model_registry=model, tool_invoker=invoker)  # type: ignore[arg-type]

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="write once",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=3),
            history=[],
        )
    ]

    recovery = next(event for event in events if event.event_type == "side_effect_unknown")
    assert model._call_index == 1
    assert invoker.invocation_count == 1
    assert recovery.data["status"] == "blocked"
    assert recovery.data["recovery_action"] == "resume"
    assert recovery.data["terminal_envelope"]["exit_reason"] == "side_effect_unknown"
    assert recovery.data["terminal_envelope"]["status"] == "blocked"
    assert recovery.data["checkpoint_id"] is None
    assert recovery.data["checkpoint_persisted"] is False
    assert recovery.data["terminal_envelope"]["resume_ready"] is False
    assert recovery.data["turn_state"]["state"] == "recovery_paused"
    assert recovery.data["terminal_envelope"]["turn_state"]["state"] == "recovery_paused"
    assert events[-1].event_type == "side_effect_unknown"
    assert all(event.event_type != "tool_call_cancelled" for event in events)
    assert all(event.event_type != "run_finished" for event in events)


@pytest.mark.asyncio
async def test_streaming_first_tool_artifact_semantic_events() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    # Iteration 1: model requests a generate_image tool call (no text).
    # Iteration 2: model responds with final text.
    tool_calls = [
        {
            "id": "tc_1",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls, "usage": {"input_tokens": 10}}],
            [{"content": "Done", "usage": {"output_tokens": 5}}],
        ]
    )

    # Tool returns output_files so AgentLoop should persist and emit artifact_created.
    png_bytes = b"hello"
    output_files = [
        {
            "filename": "x.png",
            "mime_type": "image/png",
            "size_bytes": len(png_bytes),
            "content_base64": base64.b64encode(png_bytes).decode("utf-8"),
        }
    ]
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "generate_image": {
                "success": True,
                "result": "ok",
                "duration_ms": 50.0,
                "metadata": {"duration_ms": 50.0},
                "output_files": output_files,
            }
        }
    )

    session_manager = AsyncMock()
    session_manager.add_message = AsyncMock()

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        session_manager=session_manager,  # type: ignore[arg-type]
        artifact_storage=FakeArtifactStorage(),
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=3)

    got_events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=cfg,
        history=[],
    ):
        got_events.append((ev.event_type, ev.data))

    got = [event_type for event_type, _data in got_events]

    # Tool lifecycle events
    assert "tool_call_started" in got
    assert "tool_call_completed" in got
    assert "tool_call_start" in got
    assert "tool_call_end" in got
    assert "tool_call_result" in got

    start_payload = next(data for event_type, data in got_events if event_type == "tool_call_start")
    result_payload = next(
        data for event_type, data in got_events if event_type == "tool_call_result"
    )
    end_payload = next(data for event_type, data in got_events if event_type == "tool_call_end")

    assert start_payload["tool_call_id"] == "tc_1"
    assert start_payload["name"] == "generate_image"
    assert result_payload["tool_call_id"] == "tc_1"
    assert result_payload["status"] == "completed"
    assert end_payload["tool_call_id"] == "tc_1"
    assert end_payload["status"] == "completed"

    # Manus-style step lifecycle events
    assert "step_started" in got
    assert "step_finished" in got

    # Semantic image events for UI
    assert "image_generation_start" in got
    assert "image_generation_result" in got

    # Artifact persistence + UI event
    assert "artifact_created" in got

    # Run lifecycle completeness
    assert "run_started" in got
    assert "run_finished" in got


@pytest.mark.asyncio
async def test_streaming_first_trace_activity_records_are_inspectable_and_redacted() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    tool_calls = [
        {
            "id": "tc_1",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls, "usage": {"input_tokens": 10}}],
            [{"content": "Done", "usage": {"output_tokens": 5}}],
        ]
    )
    png_bytes = b"hello"
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "generate_image": {
                "success": True,
                "result": "ok",
                "duration_ms": 50.0,
                "metadata": {"duration_ms": 50.0},
                "output_files": [
                    {
                        "filename": "x.png",
                        "mime_type": "image/png",
                        "size_bytes": len(png_bytes),
                        "content_base64": base64.b64encode(png_bytes).decode("utf-8"),
                    }
                ],
            }
        }
    )
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        artifact_storage=FakeArtifactStorage(),
    )
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate with Authorization: Bearer super-secret-value",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=3),
        history=[],
    ):
        events.append(ev)

    def first_payload(event_type: str) -> dict[str, Any]:
        payload = next(ev.data for ev in events if ev.event_type == event_type)
        assert isinstance(payload, dict)
        return payload

    run_started = first_payload("run_started")
    run_id = run_started["run_id"]
    thread_id = "s1"

    for event_type in (
        "run_started",
        "gateway_decision",
        "context_budget",
        "tool_call_start",
        "tool_call_result",
        "tool_call_end",
        "artifact_created",
        "run_finished",
    ):
        payload = first_payload(event_type)
        assert payload["run_id"] == run_id
        assert payload["thread_id"] == thread_id
        assert "super-secret-value" not in json.dumps(payload, default=str)

    artifact_payload = first_payload("artifact_created")
    assert artifact_payload["tool_call_id"] == "tc_1"
    assert artifact_payload["tool_name"] == "generate_image"


@pytest.mark.asyncio
async def test_streaming_first_approval_required_event_is_traceable() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.agent.middlewares.permission import (
        PermissionMiddleware,
        policy_from_sets,
    )
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    tool_calls = [
        {
            "id": "tc_approval",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    model = FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}]])
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=invoker,
        execution_gateway=gateway,
    )
    loop.middleware_chain.add(PermissionMiddleware(policy_from_sets(confirm={"generate_image"})))
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate with token=super-secret-value",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    ):
        events.append(ev)

    run_id = next(ev.data["run_id"] for ev in events if ev.event_type == "run_started")
    approval_payload = next(ev.data for ev in events if ev.event_type == "approval_required")

    assert approval_payload["run_id"] == run_id
    assert approval_payload["thread_id"] == "s1"
    assert approval_payload["tool_id"] == "tc_approval"
    assert approval_payload["tool_name"] == "generate_image"
    assert approval_payload["status"] == "pending"
    assert approval_payload["checkpoint_id"]
    assert approval_payload["terminal_envelope"]["exit_reason"] == "approval_pending"
    assert approval_payload["terminal_envelope"]["resume_ready"] is True
    assert approval_payload["attempt_id"].startswith("att_")
    assert approval_payload["terminal_envelope"]["attempt_id"] == approval_payload["attempt_id"]
    assert approval_payload["terminal_envelope"]["turn_state"]["state"] == "approval_paused"
    assert approval_payload["terminal_envelope"]["turn_state"]["terminal"] is False
    assert approval_payload["terminal_envelope"]["failure_decision"]["recovery_action"] == ("pause")
    assert approval_payload["context_snapshot"]["snapshot_id"].startswith("ctx_")
    assert "super-secret-value" not in json.dumps(approval_payload, default=str)


@pytest.mark.asyncio
async def test_streaming_first_confirm_pause_blocks_run_without_deny_tool_message() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.agent.middlewares.permission import (
        PermissionMiddleware,
        policy_from_sets,
    )
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    tool_calls = [
        {
            "id": "tc_approval",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    model = FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}]])
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=invoker,
        execution_gateway=gateway,
    )
    loop.middleware_chain.add(PermissionMiddleware(policy_from_sets(confirm={"generate_image"})))
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    ):
        events.append(ev)

    run_id = next(ev.data["run_id"] for ev in events if ev.event_type == "run_started")
    assert any(ev.event_type == "approval_required" for ev in events)
    assert not any(ev.event_type == "run_finished" for ev in events)
    assert invoker.invocation_count == 0

    run = await gateway.get_run(
        run_id=run_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    assert run is not None
    assert run["status"] == "blocked"


@pytest.mark.asyncio
async def test_approval_pause_keeps_trace_non_terminal_and_drains_pending_writes() -> None:
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter()
    user = MockUserContext(user_id="u1")

    run_id, _approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
        user=user,
    )

    run = await gateway.get_run(
        run_id=run_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    assert run is not None
    assert run["status"] == "blocked"
    assert writer.finished == []
    assert writer.drain_timeouts == [writer.write_timeout_s]
    assert writer.drain_strict == [True]
    assert writer.drain_trace_ids == [writer.started[0].trace_id]
    recorded_event_types = [event["event_type"] for event in writer.events]
    assert "approval_required" in recorded_event_types
    assert "run_finished" not in recorded_event_types
    assert "run_error" not in recorded_event_types


@pytest.mark.asyncio
async def test_approval_pause_strict_barrier_failure_is_fail_closed() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    tool_calls = [
        {
            "id": "tc_approval",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter(
        strict_drain_error=RuntimeError("assistant trace persistence barrier failed")
    )
    loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
    )
    events = []

    with pytest.raises(RuntimeError, match="trace persistence barrier failed"):
        async for event in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="Generate",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
            history=[],
        ):
            events.append(event)

    assert "approval_required" in [event.event_type for event in events]
    assert writer.drain_strict == [True]
    assert writer.drain_trace_ids == [writer.started[0].trace_id]
    assert writer.finished == []
    assert invoker.invocation_count == 0


@pytest.mark.asyncio
async def test_approval_resume_continues_after_persisted_trace_sequence() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter(resume_sequence=41)
    user = MockUserContext(user_id="u1")
    run_id, approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
        user=user,
    )
    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )
    assert approved is not None

    writer.operations.clear()
    writer.started.clear()
    writer.events.clear()
    writer.finished.clear()
    writer.drain_timeouts.clear()
    loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"content": "done"}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
    )
    public_events = []
    async for event in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Continue",
        config=AgentLoopConfig(
            model_id="test",
            max_tool_iterations=2,
            resume_run_id=run_id,
            resume_approval_id=approval_id,
        ),
        history=[],
    ):
        public_events.append(event)

    public_event_types = [event.event_type for event in public_events]
    resumed_run_started = next(
        event.data for event in public_events if event.event_type == "run_started"
    )
    assert writer.operations[:2] == ["resume_sequence", "start_trace"]
    assert writer.events[0]["sequence_no"] == 42
    assert all(event["sequence_no"] != 1 for event in writer.events)
    assert invoker.invocation_count == 1
    assert {"tool_call_start", "tool_call_result", "tool_call_end"}.issubset(public_event_types)
    assert resumed_run_started["attempt_number"] == 2
    assert resumed_run_started["turn_state"]["resumed_from_attempt_id"].startswith("att_")
    assert (
        resumed_run_started["attempt_id"]
        != resumed_run_started["turn_state"]["resumed_from_attempt_id"]
    )


@pytest.mark.asyncio
async def test_approval_resume_recorded_result_checkpoint_carries_exact_ack_identity() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
    from assistant_service.core.tools.tool_registry import ToolCallResult

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter()
    user = MockUserContext(user_id="u1")
    run_id, approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
        user=user,
    )
    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )
    assert approved is not None

    command_id = "99999999-9999-4999-8999-999999999999"
    gateway.invoke_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=ToolCallResult(
            call_id="approval-recorded-result",
            tool_name="generate_image",
            success=True,
            result={"ok": True},
            metadata={
                "command_id": command_id,
                "queue_state": "result_recorded_succeeded",
                "result_receipt_recorded": True,
                "result_acknowledgement_required": True,
                "result_output_files_present": False,
            },
        )
    )
    gateway.acknowledge_command_result = AsyncMock(  # type: ignore[method-assign]
        return_value={"command_id": command_id, "committed": True}
    )
    loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"content": "done", "finish_reason": "stop"}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=None,
    )

    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=user,  # type: ignore[arg-type]
            message="Continue",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=2,
                resume_run_id=run_id,
                resume_approval_id=approval_id,
            ),
            history=[],
        )
    ]

    completed = next(
        checkpoint
        for checkpoint in reversed(gateway._checkpoints[run_id])
        if checkpoint.phase == "tool_call_completed"
    )
    assert completed.pending_tool["tool_name"] == "generate_image"
    assert completed.pending_tool["arguments_hash"] == gateway._hash_value({"prompt": "cat"})
    assert completed.idempotency_keys["command_id"] == command_id
    assert completed.idempotency_keys["command_result_acknowledgeable"] is True
    gateway.acknowledge_command_result.assert_awaited_once_with(
        command_id=command_id,
        checkpoint_id=completed.checkpoint_id,
        run_id=run_id,
        tenant_id="tenant1",
        user_id="u1",
        session_id="s1",
    )
    assert any(event.event_type == "run_finished" for event in events)


@pytest.mark.asyncio
async def test_approval_resume_without_trace_writer_keeps_db_less_contract() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter()
    user = MockUserContext(user_id="u1")
    run_id, approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
        user=user,
    )
    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )
    assert approved is not None

    loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"content": "done"}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=None,
    )
    events = []
    async for event in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Continue",
        config=AgentLoopConfig(
            model_id="test",
            max_tool_iterations=2,
            resume_run_id=run_id,
            resume_approval_id=approval_id,
        ),
        history=[],
    ):
        events.append(event)

    event_types = [event.event_type for event in events]
    assert not any(
        isinstance(event.data, dict) and event.data.get("error") == "trace_resume_sequence_failed"
        for event in events
    )
    assert "run_finished" in event_types
    assert invoker.invocation_count == 1


@pytest.mark.asyncio
async def test_approval_resume_rejects_incomplete_synthesis_without_capability_claims() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter()
    user = MockUserContext(user_id="u1")
    run_id, approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
        user=user,
    )
    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )
    assert approved is not None

    model = FakeModelRegistry(
        scripted=[[{"content": "unsafe approval partial", "finish_reason": "length"}]]
    )
    loop = _confirmation_loop(
        model=model,
        invoker=invoker,
        gateway=gateway,
        trace_writer=None,
    )
    events = [
        event
        async for event in loop.execute(
            session_id="s1",
            user=user,  # type: ignore[arg-type]
            message="Continue",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=2,
                resume_run_id=run_id,
                resume_approval_id=approval_id,
                kb_dataset_ids=["private-docs"],
                kb_mode="tool",
                web_search_enabled=True,
                os_agent_enabled=True,
            ),
            history=[],
        )
    ]

    prompt = str(model.messages_history[0][0].get("content") or "")
    assert "search_knowledge_base" not in prompt
    assert "## Web Search" not in prompt
    assert "## Local OS Agent" not in prompt
    assert "## Available Tools" not in prompt
    assert model.tools_history == [None]
    assert all(
        not (event.event_type == "text_delta" and "unsafe approval partial" in str(event.data))
        for event in events
    )
    assert any(
        event.event_type == "run_error"
        and isinstance(event.data, dict)
        and event.data.get("error") == "resume_synthesis_failed"
        for event in events
    )
    assert all(event.event_type != "run_finished" for event in events)


@pytest.mark.asyncio
async def test_approval_resume_cursor_failure_stops_before_trace_and_tool() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    writer = RecordingTraceWriter(
        resume_error=RuntimeError("cursor database password=super-secret unavailable")
    )
    user = MockUserContext(user_id="u1")
    run_id, approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
        user=user,
    )
    await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )

    writer.operations.clear()
    writer.started.clear()
    writer.events.clear()
    writer.finished.clear()
    loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"content": "must not run"}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=writer,
    )
    events = []
    async for event in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Continue",
        config=AgentLoopConfig(
            model_id="test",
            max_tool_iterations=2,
            resume_run_id=run_id,
            resume_approval_id=approval_id,
        ),
        history=[],
    ):
        events.append(event)

    assert [event.event_type for event in events] == ["run_error"]
    assert events[0].data["run_id"] == run_id
    assert "super-secret" not in json.dumps(events[0].data, default=str)
    assert writer.operations == ["resume_sequence"]
    assert writer.started == []
    assert invoker.invocation_count == 0


@pytest.mark.asyncio
async def test_streaming_first_confirm_approval_resume_executes_once() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.agent.middlewares.permission import (
        PermissionMiddleware,
        policy_from_sets,
    )
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    invoker = FakeToolInvoker(results_by_name={"generate_image": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    user = MockUserContext(user_id="u1")

    def make_loop(arguments: dict[str, Any]) -> AgentLoop:
        tool_calls = [
            {
                "id": "tc_approval",
                "function": {
                    "name": "generate_image",
                    "arguments": json.dumps(arguments),
                },
            }
        ]
        model = FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}], [{"content": "done"}]])
        loop = AgentLoop(
            model_registry=model,
            tool_invoker=invoker,
            execution_gateway=gateway,
        )
        loop.middleware_chain.add(
            PermissionMiddleware(policy_from_sets(confirm={"generate_image"}))
        )
        return loop

    first_events = []
    async for ev in make_loop({"prompt": "cat"}).execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
        history=[],
    ):
        first_events.append(ev)

    first_approval = next(ev.data for ev in first_events if ev.event_type == "approval_required")
    approval_id = first_approval["approval_id"]
    run_id = next(ev.data["run_id"] for ev in first_events if ev.event_type == "run_started")
    assert approval_id
    assert invoker.invocation_count == 0

    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )
    assert approved is not None
    assert approved["status"] == "approved"

    resume_loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"content": "done"}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=None,
    )
    async for _ev in resume_loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Continue",
        config=AgentLoopConfig(
            model_id="test",
            max_tool_iterations=2,
            resume_run_id=run_id,
            resume_approval_id=approval_id,
        ),
        history=[],
    ):
        pass

    assert invoker.invocation_count == 1
    assert invoker.invocations == [("generate_image", {"prompt": "cat"})]

    duplicate_events = []
    async for ev in make_loop({"prompt": "cat", "_approval_id": approval_id}).execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
        history=[],
    ):
        duplicate_events.append(ev)

    assert invoker.invocation_count == 1
    duplicate_approval = next(
        ev.data for ev in duplicate_events if ev.event_type == "approval_required"
    )
    assert duplicate_approval["approval_id"] != approval_id


@pytest.mark.asyncio
async def test_approval_resume_redacts_unknown_side_effect_error_everywhere() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    secret = "resume-super-secret-value"
    invoker = FakeToolInvoker(
        results_by_name={
            "generate_image": {
                "success": False,
                "error": f"provider failed Authorization: Bearer {secret}",
                "metadata": {
                    "side_effect_unknown": True,
                    "tool_operation": {
                        "operation_id": "tool-op-redaction",
                        "read_back_available": True,
                        "compensation_available": False,
                    },
                    "tool_failure": {
                        "failure_kind": "side_effect_unknown",
                        "side_effect_state": "unknown",
                        "recovery_action": "resume",
                    },
                },
            }
        }
    )
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    user = MockUserContext(user_id="u1")
    run_id, approval_id = await _create_pending_approval(
        invoker=invoker,
        gateway=gateway,
        trace_writer=RecordingTraceWriter(),
        user=user,
    )
    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=True,
        approver_user_id=user.user_id,
    )
    assert approved is not None

    resume_loop = _confirmation_loop(
        model=FakeModelRegistry(scripted=[[{"content": "must not continue"}]]),
        invoker=invoker,
        gateway=gateway,
        trace_writer=None,
    )
    events = [
        event
        async for event in resume_loop.execute(
            session_id="s1",
            user=user,  # type: ignore[arg-type]
            message="Continue",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=2,
                resume_run_id=run_id,
                resume_approval_id=approval_id,
            ),
            history=[],
        )
    ]

    serialized_events = json.dumps(
        [event.data for event in events],
        default=str,
    )
    serialized_checkpoints = json.dumps(
        [gateway._checkpoint_to_dict(checkpoint) for checkpoint in gateway._checkpoints[run_id]],
        default=str,
    )
    assert invoker.invocation_count == 1
    assert events[-1].event_type == "side_effect_unknown"
    assert secret not in serialized_events
    assert secret not in serialized_checkpoints
    assert "[redacted]" in serialized_events
    assert "[redacted]" in serialized_checkpoints


@pytest.mark.asyncio
async def test_streaming_first_runs_stream_event_middleware() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    seen: list[str] = []

    class RecordingStreamMiddleware:
        name = "recording-stream"

        async def on_stream_event(self, ctx: Any, event: Any) -> Any:
            del ctx
            seen.append(event.event_type)
            if event.event_type == "text_delta":
                return replace(event, data=f"{event.data}!")
            return event

    model = FakeModelRegistry(scripted=[[{"content": "hello"}]])
    loop = AgentLoop(model_registry=model)
    loop.middleware_chain.add(RecordingStreamMiddleware())
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    ):
        events.append(ev)

    assert seen.index("run_started") < seen.index("text_delta")
    assert "text_delta" in seen
    assert seen[-1] == "run_finished"
    assert next(ev.data for ev in events if ev.event_type == "text_delta") == "hello!"


@pytest.mark.asyncio
async def test_streaming_first_rewritten_terminal_error_marks_run_failed() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    class TerminalErrorMiddleware:
        name = "terminal-error"

        async def on_stream_event(self, ctx: Any, event: Any) -> Any:
            del ctx
            if event.event_type != "run_finished":
                return None
            return replace(
                event,
                event_type="run_error",
                data={
                    "run_id": event.data["run_id"],
                    "error": "rewritten_terminal_error",
                    "reason": "Authorization: Bearer rewritten-secret-value",
                },
            )

    model = FakeModelRegistry(scripted=[[{"content": "ok"}]])
    tool_invoker = FakeToolInvoker({})
    gateway = AssistantExecutionGateway(tool_invoker=tool_invoker, database=None)
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        execution_gateway=gateway,
    )
    loop.middleware_chain.add(TerminalErrorMiddleware())
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    ):
        events.append(ev)

    event_types = [ev.event_type for ev in events]
    run_id = next(ev.data["run_id"] for ev in events if ev.event_type == "run_started")
    run_error = next(ev.data for ev in events if ev.event_type == "run_error")

    assert "run_finished" not in event_types
    assert run_error["error"] == "rewritten_terminal_error"
    assert "rewritten-secret-value" not in json.dumps(run_error, default=str)
    assert "[redacted]" in run_error["reason"]
    assert gateway._runs[run_id].status == "failed"  # AUDIT-OK: DB-less test fallback only


@pytest.mark.asyncio
async def test_dual_terminal_persistence_failure_emits_one_unknown_terminal() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    gateway = AssistantExecutionGateway(tool_invoker=FakeToolInvoker({}), database=None)
    original_save_checkpoint = gateway.save_run_checkpoint

    async def _selective_checkpoint_failure(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("phase") == "terminal_persistence_unknown":
            raise RuntimeError("checkpoint store unavailable")
        return await original_save_checkpoint(**kwargs)

    gateway.finish_run = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("run store unavailable")
    )
    gateway.save_run_checkpoint = _selective_checkpoint_failure  # type: ignore[method-assign]
    loop = AgentLoop(
        model_registry=FakeModelRegistry(scripted=[[{"content": "done"}]]),
        tool_invoker=FakeToolInvoker({}),  # type: ignore[arg-type]
        execution_gateway=gateway,
    )

    events = [
        event
        async for event in loop.execute(
            session_id="s-terminal-dual-failure",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="hello",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        )
    ]
    terminals = [event for event in events if event.event_type in {"run_finished", "run_error"}]

    assert len(terminals) == 1
    assert terminals[0].event_type == "run_error"
    assert terminals[0].data["error"] == "terminal_persistence_unknown"
    assert terminals[0].data["persistence"]["finish_committed"] is False
    assert terminals[0].data["persistence"]["checkpoint_committed"] is False
    assert all(event.event_type != "run_finished" for event in events)


@pytest.mark.asyncio
async def test_authoritative_terminal_conflict_never_appends_unknown_checkpoint() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    class TerminalErrorMiddleware:
        name = "terminal-error"

        async def on_stream_event(self, ctx: Any, event: Any) -> Any:
            del ctx
            if event.event_type != "run_finished":
                return None
            return replace(
                event,
                event_type="run_error",
                data={"run_id": event.data["run_id"], "error": "rewritten_failure"},
            )

    gateway = AssistantExecutionGateway(tool_invoker=FakeToolInvoker({}), database=None)
    saved_phases: list[str] = []
    original_save_checkpoint = gateway.save_run_checkpoint

    async def _record_checkpoint(**kwargs: Any) -> dict[str, Any]:
        saved_phases.append(str(kwargs.get("phase") or ""))
        return await original_save_checkpoint(**kwargs)

    gateway.finish_run = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "run_id": "external-run",
            "status": "succeeded",
            "committed": False,
            "durability": "database",
            "authoritative_terminal": True,
            "hard_checkpoint": {
                "checkpoint_id": "hard-terminal-checkpoint",
                "phase": "run_succeeded",
            },
        }
    )
    gateway.save_run_checkpoint = _record_checkpoint  # type: ignore[method-assign]
    loop = AgentLoop(
        model_registry=FakeModelRegistry(scripted=[[{"content": "done"}]]),
        tool_invoker=FakeToolInvoker({}),  # type: ignore[arg-type]
        execution_gateway=gateway,
    )
    loop.middleware_chain.add(TerminalErrorMiddleware())

    events = [
        event
        async for event in loop.execute(
            session_id="s-terminal-authoritative-conflict",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="hello",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        )
    ]
    terminals = [event for event in events if event.event_type in {"run_finished", "run_error"}]

    assert len(terminals) == 1
    assert terminals[0].data["error"] == "authoritative_terminal_conflict"
    assert terminals[0].data["persistence"]["authoritative_terminal"] is True
    assert "terminal_persistence_unknown" not in saved_phases


@pytest.mark.asyncio
async def test_streaming_first_runs_error_middleware() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig, AgentLoopEvent

    seen: list[str] = []

    class RecordingErrorMiddleware:
        name = "recording-error"

        async def on_error(self, ctx: Any, error: BaseException, phase: Any):
            seen.append(str(error))
            yield AgentLoopEvent(
                phase=phase,
                event_type="middleware_error_seen",
                data={"run_id": ctx.run_id},
            )

    model = FakeFailingModelRegistry("provider failure")
    loop = AgentLoop(model_registry=model)
    loop.middleware_chain.add(RecordingErrorMiddleware())
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    ):
        events.append(ev)

    assert seen == ["provider failure"]
    assert any(ev.event_type == "middleware_error_seen" for ev in events)
    assert any(ev.event_type == "error" for ev in events)


@pytest.mark.asyncio
async def test_streaming_first_run_error_event_is_traceable_and_redacted() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    model = FakeFailingModelRegistry(
        "provider failure with Authorization: Bearer super-secret-value"
    )
    loop = AgentLoop(model_registry=model)
    user = MockUserContext(user_id="u1")

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Hi",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    ):
        events.append(ev)

    run_id = next(ev.data["run_id"] for ev in events if ev.event_type == "run_started")
    run_error_payload = next(ev.data for ev in events if ev.event_type == "run_error")
    serialized_events = json.dumps([ev.to_dict() for ev in events], default=str)

    assert run_error_payload["run_id"] == run_id
    assert run_error_payload["thread_id"] == "s1"
    assert "[redacted]" in run_error_payload["error"]
    assert run_error_payload["attempt_id"].startswith("att_")
    assert run_error_payload["terminal_envelope"]["attempt_id"] == run_error_payload["attempt_id"]
    assert run_error_payload["terminal_envelope"]["turn_state"]["state"] == "failed"
    assert run_error_payload["terminal_envelope"]["failure_decision"] == {
        "failure_class": "model_error",
        "retry_safety": "safe",
        "recovery_action": "retry",
        "user_visibility": "warning",
        "side_effect_state": "none",
        "recoverable": True,
    }
    assert sum(ev.event_type == "run_error" for ev in events) == 1
    assert "super-secret-value" not in serialized_events


@pytest.mark.asyncio
async def test_streaming_first_kb_panel_events() -> None:
    """KB tool call must emit ``context_retrieved`` for the panel UI.

    Web panel events were removed in PR-2 along with the in-tree Tavily
    ``search_web`` tool — capable models do their own search and there
    is no display payload to forward.
    """
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    tool_calls = [
        {
            "id": "kb_1",
            "function": {
                "name": "search_knowledge_base",
                "arguments": '{"query":"x","dataset_ids":["d1"]}',
            },
        },
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls}],
            [{"content": "ok"}],
        ]
    )

    kb_ctx = {
        "dataset_id": "d1",
        "dataset_name": "D1",
        "chunks": [{"content": "c", "score": 0.9}],
        "query": "x",
        "took_ms": 12.0,
    }

    tool_invoker = FakeToolInvoker(
        results_by_name={
            "search_knowledge_base": {
                "success": True,
                "result": "kb",
                "metadata": {"total_results": 1, "contexts": [kb_ctx]},
            },
        }
    )

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=3)

    got = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="q",
        config=cfg,
        history=[],
    ):
        got.append(ev.event_type)

    assert "context_retrieved" in got


@pytest.mark.asyncio
async def test_streaming_first_skips_duplicate_kb_calls_in_same_turn() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    duplicate_kb_calls = [
        {
            "id": "kb_1",
            "function": {
                "name": "search_knowledge_base",
                "arguments": '{"query":"policy","dataset_ids":["d1"]}',
            },
        },
        {
            "id": "kb_2",
            "function": {
                "name": "search_knowledge_base",
                "arguments": '{"query":"policy","dataset_ids":["d1"]}',
            },
        },
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": duplicate_kb_calls}],
            [{"content": "done"}],
        ]
    )
    kb_ctx = {
        "dataset_id": "d1",
        "dataset_name": "D1",
        "chunks": [{"content": "snippet", "score": 0.88}],
        "query": "policy",
        "took_ms": 10.0,
    }
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "search_knowledge_base": {
                "success": True,
                "result": "kb",
                "metadata": {"total_results": 1, "contexts": [kb_ctx]},
            }
        }
    )

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=3)

    got = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="policy question",
        config=cfg,
        history=[],
    ):
        got.append(ev.event_type)

    assert tool_invoker.invocation_count == 1
    assert got.count("step_started") == 1


@pytest.mark.asyncio
async def test_streaming_first_merges_chunked_tool_calls_before_execute() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    chunked_tool_calls = [
        {
            "index": 0,
            "id": "kb_chunked_1",
            "type": "function",
            "function": {
                "name": "search_knowledge_base",
                "arguments": '{"query":"穆斯林饮食禁忌"',
            },
        },
        {
            "index": 0,
            "type": "function",
            "function": {"arguments": ',"dataset_ids":["d1"]}'},
        },
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": [chunked_tool_calls[0]]}, {"tool_calls": [chunked_tool_calls[1]]}],
            [{"content": "ok"}],
        ]
    )
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "search_knowledge_base": {
                "success": True,
                "result": "kb",
                "metadata": {"total_results": 1, "contexts": []},
            }
        }
    )

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=3)

    events = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="q",
        config=cfg,
        history=[],
    ):
        events.append(ev.event_type)

    assert tool_invoker.invocation_count == 1
    assert tool_invoker.invocations[0][0] == "search_knowledge_base"
    assert tool_invoker.invocations[0][1]["query"] == "穆斯林饮食禁忌"
    assert tool_invoker.invocations[0][1]["dataset_ids"] == ["d1"]
    assert events.count("step_started") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_arguments",
    ['{"path":', '{"amount":NaN}', '{"amount":Infinity}', '{"amount":1e309}'],
)
async def test_streaming_first_rejects_malformed_tool_arguments_without_execution(
    malformed_arguments: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed provider arguments remain paired but never cross the tool boundary."""
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    untrusted_tool_name = "private-tool-name\nforged-log-entry"
    model = FakeModelRegistry(
        scripted=[
            [
                {
                    "tool_calls": [
                        {
                            "id": "bad_args_1",
                            "type": "function",
                            "function": {
                                "name": untrusted_tool_name,
                                "arguments": malformed_arguments,
                            },
                        }
                    ]
                }
            ],
            [{"content": "recovered"}],
        ]
    )
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "demo_write": {
                "success": True,
                "result": "must-not-run",
            }
        }
    )
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )

    events = []
    async for event in loop.execute(
        session_id="s1",
        user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
        message="write this",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=3),
        history=[],
    ):
        events.append(event.event_type)

    assert tool_invoker.invocation_count == 0
    assert tool_invoker.invocations == []
    assert model._call_index == 2
    assert model.last_messages is not None
    assistant_messages = [
        message
        for message in model.last_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    tool_messages = [message for message in model.last_messages if message.get("role") == "tool"]
    assert len(assistant_messages) == 1
    assert len(tool_messages) == 1
    assert assistant_messages[0]["tool_calls"][0]["id"] == "bad_args_1"
    assert assistant_messages[0]["tool_calls"][0]["function"]["name"] == untrusted_tool_name
    assert assistant_messages[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert tool_messages[0]["tool_call_id"] == "bad_args_1"
    assert tool_messages[0]["name"] == untrusted_tool_name
    assert "tool call rejected" in tool_messages[0]["content"]
    assert "no tool was executed" in tool_messages[0]["content"]
    assert not {
        "tool_call_started",
        "tool_call_completed",
        "tool_call_start",
        "tool_call_result",
        "tool_call_end",
        "step_started",
    }.intersection(events)
    assert "run_finished" in events
    assert untrusted_tool_name not in caplog.text
    assert "unrecognized_tool_sha256:" in caplog.text


@pytest.mark.asyncio
async def test_policy_denied_unrecognized_tool_name_is_hash_only_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.agent.middleware import ToolVerdict

    sentinel = "private-unknown-tool\nforged-log-entry"
    model = FakeModelRegistry(
        scripted=[
            [
                {
                    "tool_calls": [
                        {
                            "id": "unknown_1",
                            "type": "function",
                            "function": {"name": sentinel, "arguments": "{}"},
                        }
                    ]
                }
            ],
            [{"content": "recovered"}],
        ]
    )
    tool_invoker = FakeToolInvoker(
        results_by_name={"demo_write": {"success": True, "result": "unused"}}
    )
    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )

    class _DenyMiddleware:
        name = "deny-test"

        async def on_tool_call(self, _ctx, tool_name, _arguments):
            return ToolVerdict.deny(
                reason=f"tool {tool_name!r} is denied by policy",
                source="permission",
            )

    loop.middleware_chain.add(_DenyMiddleware())  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO):
        async for _ in loop.execute(
            session_id="s1",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="try unknown",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=3),
            history=[],
        ):
            pass

    assert tool_invoker.invocation_count == 0
    assert sentinel not in caplog.text
    assert "unrecognized_tool_sha256:" in caplog.text
    assert "reason_sha256=" in caplog.text


@pytest.mark.asyncio
async def test_streaming_first_dedups_batch_level_duplicate_tool_calls() -> None:
    """Regression: Activity drawer showed `generate_quiz` twice for a single
    logical call (first observed on Gemini 3 Flash, 2026-04-22).

    Root cause, in general terms: provider streaming can land the same
    logical tool call in two accumulator slots — e.g. one chunk keys on
    `index`, another on `id`, or partial-args chunks split across frames
    — so `merge_stream_tool_calls` flushes two batch entries with
    identical name+args. Each gets a fresh `tool_id` downstream, so the
    tool (e.g. `generate_quiz`) runs twice.

    The fix is a provider-agnostic dedup right after the accumulator has
    fully assembled each call. This test simulates the `index`-only vs
    `id`-only chunk split to pin that dedup in place, but the real
    defense is structural — any provider that produces two batch entries
    with the same `(name, args)` after merge is deduped here.

    Asserts:
      * the tool runs exactly once
      * exactly one `step_started` / `tool_call_started` pair is emitted
    """
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    quiz_args = (
        '{"title":"Quiz","questions":[{"question_num":1,'
        '"question_type":"mc_single",'
        '"question_text":"Q?","options":[{"label":"A","text":"a"},'
        '{"label":"B","text":"b"}],"correct_answer":["A"],'
        '"explanation":"because"}]}'
    )

    # Two chunks for the SAME logical call — first carries only `index`,
    # second carries only `id`. Args are fully present in each to simulate
    # the provider re-emitting the call in the finish chunk.
    chunk_with_index_only = [
        {
            "index": 0,
            "type": "function",
            "function": {"name": "generate_quiz", "arguments": quiz_args},
        }
    ]
    chunk_with_id_only = [
        {
            "id": "call_quiz_abc123",
            "type": "function",
            "function": {"name": "generate_quiz", "arguments": quiz_args},
        }
    ]
    model = FakeModelRegistry(
        scripted=[
            [
                {"tool_calls": chunk_with_index_only},
                {"tool_calls": chunk_with_id_only},
            ],
            [{"content": "Quiz ready"}],
        ]
    )
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "generate_quiz": {
                "success": True,
                "result": "Quiz 'Quiz' created with 1 questions.",
                "duration_ms": 25.0,
                "metadata": {"quiz_data": {"quiz_id": "q1", "title": "Quiz"}},
            }
        }
    )

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=3)

    events: list[str] = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="make me a quiz",
        config=cfg,
        history=[],
    ):
        events.append(ev.event_type)

    assert tool_invoker.invocation_count == 1, (
        "generate_quiz should run exactly once despite two accumulator entries on the wire"
    )
    assert events.count("tool_call_started") == 1
    assert events.count("step_started") == 1
    # Exactly one quiz_ready (the Activity drawer and the quiz card must
    # agree — two would overwrite the rendered card while leaving two pills).
    assert events.count("quiz:ready") == 1


@pytest.mark.asyncio
async def test_streaming_first_batch_dedup_preserves_distinct_tool_calls() -> None:
    """Guardrail: the batch-level dedup must NOT collapse genuinely
    different tool calls. Same tool name with different args is legitimate
    (e.g. two generate_image prompts, or two search_knowledge_base queries
    with distinct dataset_ids)."""
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    tool_calls = [
        {
            "index": 0,
            "id": "call_a",
            "type": "function",
            "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'},
        },
        {
            "index": 1,
            "id": "call_b",
            "type": "function",
            "function": {"name": "generate_image", "arguments": '{"prompt":"dog"}'},
        },
    ]
    model = FakeModelRegistry(
        scripted=[
            [{"tool_calls": tool_calls}],
            [{"content": "done"}],
        ]
    )
    tool_invoker = FakeToolInvoker(
        results_by_name={
            "generate_image": {
                "success": True,
                "result": "ok",
                "duration_ms": 10.0,
            }
        }
    )

    loop = AgentLoop(
        model_registry=model,
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
    )
    user = MockUserContext(user_id="u1")
    cfg = AgentLoopConfig(model_id="test", max_tool_iterations=3)

    events: list[str] = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="two images",
        config=cfg,
        history=[],
    ):
        events.append(ev.event_type)

    # Both distinct calls should execute — dedup only collapses same-args.
    assert tool_invoker.invocation_count == 2
    assert events.count("tool_call_started") == 2


@pytest.mark.asyncio
async def test_memory_off_restores_only_scoped_working_state() -> None:
    from ai_gateway_core.tasks.task_manager import TaskManager
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.runtime.memory.working_state import persist_working_memory
    from assistant_service.core.working_memory import WorkingMemory

    class ScopedMemoryService:
        def __init__(self) -> None:
            self.payloads: dict[str, Any] = {}
            self.long_term_reads = 0
            self.user_memory_calls = 0
            self.set_session_calls = 0

        async def get_session_memory(self, **kwargs: Any) -> Any:
            return self.payloads.get(str(kwargs["key"]))

        async def set_session_memory(self, **kwargs: Any) -> bool:
            self.set_session_calls += 1
            self.payloads[str(kwargs["key"])] = kwargs["value"]
            return True

        async def get_long_term_context(self, **_kwargs: Any) -> dict[str, Any]:
            self.long_term_reads += 1
            return {"preferences": {"must_not": "load"}}

        async def get_user_memory(self, **_kwargs: Any) -> Any:
            self.user_memory_calls += 1
            return None

        async def set_user_memory(self, **_kwargs: Any) -> bool:
            self.user_memory_calls += 1
            return True

    class RuntimeWithMemoryEnabled:
        features = SimpleNamespace(skills=False, memory_v2=True, context_v2=False)

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def load_memory_context(self, **_kwargs: Any) -> Any:
            self.calls.append("load")
            raise AssertionError("memory retrieval must stay off")

        async def schedule_daily_reflection(self, **_kwargs: Any) -> Any:
            self.calls.append("reflect")
            raise AssertionError("memory reflection must stay off")

        async def sync_turn_to_memory(self, **_kwargs: Any) -> Any:
            self.calls.append("sync")
            raise AssertionError("memory sync must stay off")

    service = ScopedMemoryService()
    working = WorkingMemory(session_id="uao04-working-restore")
    working.set_goal("preserve the unresolved recovery plan")
    working.add_task("readback", "read back external state")
    assert await persist_working_memory(
        service,
        tenant_id="tenant1",
        user_id="u1",
        session_id="uao04-working-restore",
        memory=working,
    )
    service.set_session_calls = 0

    runtime = RuntimeWithMemoryEnabled()
    model = FakeModelRegistry(scripted=[[{"content": "continued safely"}]])
    loop = AgentLoop(
        model_registry=model,
        memory_service=service,  # type: ignore[arg-type]
        runtime_adapter=runtime,  # type: ignore[arg-type]
        task_manager=TaskManager(),
    )
    events = [
        event
        async for event in loop.execute(
            session_id="uao04-working-restore",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="continue",
            config=AgentLoopConfig(
                model_id="test",
                memory_mode="off",
                memory_profile="hybrid",
                runtime_mode="full",
                skills_enabled=False,
                kb_mode="off",
            ),
            history=[],
        )
    ]

    prompt = json.dumps(model.last_messages, ensure_ascii=False)
    assert "preserve the unresolved recovery plan" in prompt
    assert "read back external state" in prompt
    assert service.long_term_reads == 0
    assert service.user_memory_calls == 0
    # No durable owner proof is available in this direct-loop compatibility
    # test, so only the owner-bound v2 state may be refreshed.
    assert service.set_session_calls == 1
    assert runtime.calls == []
    completed = next(
        event.data for event in events if event.event_type == "streaming_first_completed"
    )
    assert completed["memory_sync"] == {
        "synced": False,
        "skipped": True,
        "reason": "memory_policy_off",
    }


def test_resume_ready_requires_matching_persisted_approval_checkpoint() -> None:
    from assistant_service.core.agent.agent_loop import (
        AgentLoop,
        AgentLoopConfig,
        AgentLoopContext,
    )

    loop = AgentLoop(model_registry=FakeModelRegistry(scripted=[]))
    ctx = AgentLoopContext(
        session_id="s-resume",
        user_id="u1",
        tenant_id="tenant1",
        message="approve",
        config=AgentLoopConfig(model_id="test"),
    )
    loop._initialize_turn_kernel(ctx)
    ctx.approval_paused = True
    ctx.last_approval_id = "approval-1"
    ctx.last_checkpoint_id = "checkpoint-1"
    ctx.last_checkpoint_phase = "run_started"

    stale = loop._terminal_envelope(
        ctx,
        status="blocked",
        exit_reason="approval_pending",
    )
    assert stale["resume_ready"] is False
    assert stale.get("checkpoint_id") is None

    ctx.last_checkpoint_phase = "approval_pending"
    ready = loop._terminal_envelope(
        ctx,
        status="blocked",
        exit_reason="approval_pending",
    )
    assert ready["resume_ready"] is True
    assert ready["checkpoint_id"] == "checkpoint-1"

    ctx.approval_paused = False
    ctx.recovery_paused = True
    ctx.last_checkpoint_phase = "side_effect_unknown"
    recovery = loop._terminal_envelope(
        ctx,
        status="blocked",
        exit_reason="side_effect_unknown",
    )
    assert recovery["resume_ready"] is False


@pytest.mark.asyncio
async def test_approval_checkpoint_failure_never_claims_resume_ready() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.agent.middlewares.permission import (
        PermissionMiddleware,
        policy_from_sets,
    )
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    tool_calls = [
        {
            "id": "tc_checkpoint_failure",
            "function": {"name": "external_write", "arguments": '{"value":"x"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"external_write": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    loop = AgentLoop(
        model_registry=FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}]]),
        tool_invoker=invoker,  # type: ignore[arg-type]
        execution_gateway=gateway,
    )
    loop.middleware_chain.add(PermissionMiddleware(policy_from_sets(confirm={"external_write"})))
    loop._save_checkpoint = AsyncMock(return_value=None)  # type: ignore[method-assign]

    events = [
        event
        async for event in loop.execute(
            session_id="s-checkpoint-failure",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="write",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        )
    ]

    failure = next(
        event.data
        for event in events
        if event.event_type == "run_error"
        and event.data.get("error") == "checkpoint_persistence_failed"
    )
    assert failure["terminal_envelope"]["resume_ready"] is False
    assert failure["terminal_envelope"].get("checkpoint_id") is None
    assert all(event.event_type != "approval_required" for event in events)
    assert invoker.invocation_count == 0


@pytest.mark.asyncio
async def test_tool_dispatch_checkpoint_failure_blocks_write_before_invocation() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway

    tool_calls = [
        {
            "id": "tc_write_fence_failure",
            "function": {"name": "external_write", "arguments": '{"value":"x"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"external_write": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    loop = AgentLoop(
        model_registry=FakeModelRegistry(scripted=[[{"tool_calls": tool_calls}]]),
        tool_invoker=invoker,  # type: ignore[arg-type]
        execution_gateway=gateway,
    )
    loop._save_checkpoint = AsyncMock(return_value=None)  # type: ignore[method-assign]

    events = [
        event
        async for event in loop.execute(
            session_id="s-write-fence-failure",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="write",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        )
    ]

    failure = next(
        event.data
        for event in events
        if event.event_type == "run_error"
        and event.data.get("error") == "checkpoint_persistence_failed"
    )
    assert failure["tool_id"] == "tc_write_fence_failure"
    assert failure["recoverable"] is False
    assert failure["terminal_envelope"]["resume_ready"] is False
    assert invoker.invocation_count == 0


@pytest.mark.asyncio
async def test_recorded_command_is_acked_only_after_completed_checkpoint() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
    from assistant_service.core.tools.tool_registry import ToolCallResult

    tool_calls = [
        {
            "id": "tc_result_ack",
            "function": {"name": "external_write", "arguments": '{"value":"x"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"external_write": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    command_id = "88888888-8888-4888-8888-888888888888"
    gateway.invoke_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=ToolCallResult(
            call_id="gateway-result",
            tool_name="external_write",
            success=True,
            result={"ok": True},
            metadata={
                "command_id": command_id,
                "queue_state": "result_recorded_succeeded",
                "result_receipt_recorded": True,
                "result_acknowledgement_required": True,
                "result_output_files_present": False,
            },
        )
    )
    gateway.acknowledge_command_result = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "command_id": command_id,
            "committed": True,
            "durability": "process",
        }
    )
    loop = AgentLoop(
        model_registry=FakeModelRegistry(
            scripted=[
                [{"tool_calls": tool_calls}],
                [{"content": "done", "finish_reason": "stop"}],
            ]
        ),
        tool_invoker=invoker,  # type: ignore[arg-type]
        execution_gateway=gateway,
    )

    events = [
        event
        async for event in loop.execute(
            session_id="s-result-ack",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="write",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
            history=[],
        )
    ]

    completed = next(
        checkpoint
        for checkpoints in gateway._checkpoints.values()  # AUDIT-OK: DB-less contract fixture
        for checkpoint in checkpoints
        if checkpoint.phase == "tool_call_completed"
    )
    assert completed.idempotency_keys["command_id"] == command_id
    assert completed.idempotency_keys["command_result_acknowledgeable"] is True
    assert completed.resume_payload["_checkpoint_receipt"]["committed"] is True
    gateway.acknowledge_command_result.assert_awaited_once_with(
        command_id=command_id,
        checkpoint_id=completed.checkpoint_id,
        run_id=completed.run_id,
        tenant_id="tenant1",
        user_id="u1",
        session_id="s-result-ack",
    )
    assert any(event.event_type == "run_finished" for event in events)
    assert invoker.invocation_count == 0


@pytest.mark.asyncio
async def test_external_synthetic_artifact_handle_is_never_command_ack_proof() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig
    from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
    from assistant_service.core.tools.tool_registry import ToolCallResult

    tool_calls = [
        {
            "id": "tc_external_artifact",
            "function": {"name": "external_write", "arguments": '{"value":"x"}'},
        }
    ]
    invoker = FakeToolInvoker(results_by_name={"external_write": {"success": True}})
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    command_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    gateway.invoke_tool = AsyncMock(  # type: ignore[method-assign]
        return_value=ToolCallResult(
            call_id="external-artifact-result",
            tool_name="external_write",
            success=True,
            result={"ok": True},
            metadata={
                "command_id": command_id,
                "queue_state": "result_recorded_succeeded",
                "result_receipt_recorded": True,
                "result_acknowledgement_required": True,
                "result_output_files_present": True,
            },
            output_files=[
                {
                    "filename": "remote.txt",
                    "mime_type": "text/plain",
                    "download_url": "https://files.example.invalid/remote.txt",
                    "externally_hosted": True,
                }
            ],
        )
    )
    gateway.acknowledge_command_result = AsyncMock()  # type: ignore[method-assign]
    loop = AgentLoop(
        model_registry=FakeModelRegistry(
            scripted=[
                [{"tool_calls": tool_calls}],
                [{"content": "done", "finish_reason": "stop"}],
            ]
        ),
        tool_invoker=invoker,  # type: ignore[arg-type]
        execution_gateway=gateway,
        # A truthy storage object reaches the externally-hosted branch without
        # invoking storage methods and reproduces its synthetic `ext-*` id.
        artifact_storage=object(),
    )

    events = [
        event
        async for event in loop.execute(
            session_id="s-external-artifact",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="write",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
            history=[],
        )
    ]

    completed = next(
        checkpoint
        for checkpoints in gateway._checkpoints.values()
        for checkpoint in checkpoints
        if checkpoint.phase == "tool_call_completed"
    )
    assert completed.idempotency_keys["command_result_acknowledgeable"] is False
    assert completed.resume_payload["artifact_receipt_complete"] is False
    assert completed.resume_payload["output_artifact_ids"] == []
    gateway.acknowledge_command_result.assert_not_awaited()
    artifact_event = next(event for event in events if event.event_type == "artifact_created")
    assert str(artifact_event.data["artifact_id"]).startswith("ext-")
