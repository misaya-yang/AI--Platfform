"""
Contract tests for AgentLoop Streaming-First mode.

Goal: ensure the AgentLoop streaming-first path emits the minimum set of events
required by the Assistant UI (Manus-style task/tool/artifact visualization).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from assistant_service.core.agent.agent_loop import _redact_trace_text


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

    def get_model(self, _model_id: str) -> Any:
        return FakeModelInfo()

    async def chat_stream(self, *_args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        from assistant_service.core.models.model_registry import StreamDelta

        # Capture the prompt/messages passed by AgentLoop for assertions.
        self.last_messages = kwargs.get("messages")

        idx = self._call_index
        self._call_index += 1
        deltas = self._scripted[idx] if idx < len(self._scripted) else []
        for d in deltas:
            yield StreamDelta(
                content=d.get("content", ""),
                tool_calls=d.get("tool_calls"),
                usage=d.get("usage"),
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
    loop.middleware_chain.add(
        PermissionMiddleware(policy_from_sets(confirm={"generate_image"}))
    )
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
    assert completed["terminal_envelope"]["exit_reason"] == "succeeded"
    assert completed["terminal_envelope"]["context_snapshot_id"].startswith("ctx_")
    assert run_finished["terminal_envelope"]["status"] == "succeeded"
    assert run_finished["terminal_envelope"]["thread_id"] == "s1"
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
        context, user  # type: ignore[arg-type]
    )
    knowledge.rows = [
        {
            **knowledge.rows[0],
            "updated_at": "2026-07-14T01:00:00Z",
        }
    ]
    _, second_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context, user  # type: ignore[arg-type]
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

    start_payload = next(
        data for event_type, data in got_events if event_type == "tool_call_start"
    )
    result_payload = next(
        data for event_type, data in got_events if event_type == "tool_call_result"
    )
    end_payload = next(
        data for event_type, data in got_events if event_type == "tool_call_end"
    )

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
    loop.middleware_chain.add(
        PermissionMiddleware(policy_from_sets(confirm={"generate_image"}))
    )
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
    loop.middleware_chain.add(
        PermissionMiddleware(policy_from_sets(confirm={"generate_image"}))
    )
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
    public_event_types = []
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
        public_event_types.append(event.event_type)

    assert writer.operations[:2] == ["resume_sequence", "start_trace"]
    assert writer.events[0]["sequence_no"] == 42
    assert all(event["sequence_no"] != 1 for event in writer.events)
    assert invoker.invocation_count == 1
    assert {"tool_call_start", "tool_call_result", "tool_call_end"}.issubset(
        public_event_types
    )


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
        isinstance(event.data, dict)
        and event.data.get("error") == "trace_resume_sequence_failed"
        for event in events
    )
    assert "run_finished" in event_types
    assert invoker.invocation_count == 1


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
        model = FakeModelRegistry(
            scripted=[[{"tool_calls": tool_calls}], [{"content": "done"}]]
        )
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

    first_approval = next(
        ev.data for ev in first_events if ev.event_type == "approval_required"
    )
    approval_id = first_approval["approval_id"]
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

    async for _ev in make_loop({"prompt": "cat", "_approval_id": approval_id}).execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
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
                data={"run_id": event.data["run_id"], "error": "rewritten_terminal_error"},
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
    assert gateway._runs[run_id].status == "failed"  # AUDIT-OK: DB-less test fallback only


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
        "generate_quiz should run exactly once despite two accumulator "
        "entries on the wire"
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
