from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class _Ctx:
    trace_started_at: float = field(default_factory=time.time)
    generated_content: str = ""


@dataclass
class _Event:
    event_type: str
    data: dict[str, Any]


@pytest.mark.asyncio
async def test_call_limit_middleware_denies_after_limit() -> None:
    from assistant_service.core.agent.middleware import VerdictKind
    from assistant_service.core.agent.middlewares.harness import CallLimitMiddleware

    ctx = _Ctx()
    middleware = CallLimitMiddleware(max_calls=1)

    assert (await middleware.on_tool_call(ctx, "search", {})).kind is VerdictKind.ALLOW
    assert (await middleware.on_tool_call(ctx, "search", {})).kind is VerdictKind.DENY


@pytest.mark.asyncio
async def test_loop_detection_middleware_denies_repeated_fingerprint() -> None:
    from assistant_service.core.agent.middleware import VerdictKind
    from assistant_service.core.agent.middlewares.harness import LoopDetectionMiddleware

    ctx = _Ctx()
    middleware = LoopDetectionMiddleware(max_repeats=1)

    assert (
        await middleware.on_tool_call(ctx, "search", {"query": "x"})
    ).kind is VerdictKind.ALLOW
    assert (
        await middleware.on_tool_call(ctx, "search", {"query": "x"})
    ).kind is VerdictKind.DENY
    assert (
        await middleware.on_tool_call(ctx, "search", {"query": "y"})
    ).kind is VerdictKind.ALLOW


@pytest.mark.asyncio
async def test_time_budget_middleware_denies_after_budget() -> None:
    from assistant_service.core.agent.middleware import VerdictKind
    from assistant_service.core.agent.middlewares.harness import TimeBudgetMiddleware

    ctx = _Ctx(trace_started_at=time.time() - 10)
    middleware = TimeBudgetMiddleware(max_seconds=1)

    assert (await middleware.on_tool_call(ctx, "search", {})).kind is VerdictKind.DENY


@pytest.mark.asyncio
async def test_precompletion_checklist_rewrites_empty_success() -> None:
    from assistant_service.core.agent.middlewares.harness import (
        PreCompletionChecklistMiddleware,
    )

    middleware = PreCompletionChecklistMiddleware()
    event = _Event(event_type="run_finished", data={"run_id": "r1"})

    rewritten = await middleware.on_stream_event(_Ctx(generated_content=""), event)

    assert rewritten is not None
    assert rewritten.event_type == "run_error"
    assert rewritten.data["error"] == "empty_assistant_response"


@pytest.mark.asyncio
async def test_trace_sensor_records_stream_events_and_errors() -> None:
    from assistant_service.core.agent.middlewares.harness import TraceSensorMiddleware

    ctx = _Ctx()
    middleware = TraceSensorMiddleware()

    await middleware.on_stream_event(ctx, _Event(event_type="run_started", data={}))
    async for _event in middleware.on_error(ctx, RuntimeError("boom"), "generation"):
        pass

    assert ctx._middleware_trace_events == ["run_started"]
    assert ctx._middleware_trace_errors == [
        {"phase": "generation", "error_type": "RuntimeError"}
    ]
