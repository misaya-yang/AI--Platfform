"""
Contract tests for AgentLoop Streaming-First mode.

Goal: ensure the AgentLoop streaming-first path emits the minimum set of events
required by the Assistant UI (Manus-style task/tool/artifact visualization).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest


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

    def get_model(self, model_id: str) -> Any:
        return FakeModelInfo()

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
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


class FakeArtifact:
    def __init__(self, artifact_id: str):
        self.artifact_id = artifact_id


class FakeArtifactStorage:
    async def create_artifact(self, **kwargs: Any) -> Any:
        return FakeArtifact("art_123")

    async def get_presigned_download_url(self, artifact: Any, expiry_seconds: int = 3600) -> str:
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
async def test_streaming_first_system_prompt_keeps_base_prompt() -> None:
    """
    Regression: frontend may send a style-only system_prompt.
    Streaming-first must keep the base tool/KR instructions and append extra.
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
    assert "Additional System Instructions" in sys_content
    assert "STYLE_ONLY_PROMPT" in sys_content


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

    got = []
    async for ev in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="Generate",
        config=cfg,
        history=[],
    ):
        got.append(ev.event_type)

    # Tool lifecycle events
    assert "tool_call_started" in got
    assert "tool_call_completed" in got

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
