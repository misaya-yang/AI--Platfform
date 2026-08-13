from __future__ import annotations

import inspect
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


def _build_default_chain():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = object.__new__(AgentLoop)
    loop.assistant_runtime = None
    loop.artifact_storage = None
    return loop._build_default_middleware_chain()


def test_agent_loop_registers_reliability_middlewares_in_default_order() -> None:
    chain = _build_default_chain()

    assert [middleware.name for middleware in chain.middlewares] == [
        "time-budget",
        "call-limit",
        "loop-detection",
        "runtime_memory",
        "tool_output_spill",
        "response_cap",
        "pre-completion-checklist",
        "trace-sensor",
    ]


@pytest.mark.asyncio
async def test_default_chain_denies_fourth_identical_tool_call() -> None:
    from assistant_service.core.agent.middleware import VerdictKind

    ctx = _Ctx()
    chain = _build_default_chain()

    verdicts = [
        await chain.run_on_tool_call(ctx, "search", {"query": "same"})
        for _ in range(4)
    ]

    assert [verdict.kind for verdict in verdicts[:3]] == [VerdictKind.ALLOW] * 3
    assert verdicts[3].kind is VerdictKind.DENY
    assert verdicts[3].source == "loop-detection"


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
async def test_loop_detection_stores_only_hashed_fingerprints() -> None:
    from assistant_service.core.agent.middlewares.harness import LoopDetectionMiddleware

    ctx = _Ctx()
    secret = "tenant-private-query"

    await LoopDetectionMiddleware(max_repeats=3).on_tool_call(
        ctx,
        "search",
        {"query": secret},
    )

    fingerprints = list(ctx._middleware_tool_fingerprints)
    assert len(fingerprints) == 1
    assert len(fingerprints[0]) == 64
    assert all(character in "0123456789abcdef" for character in fingerprints[0])
    assert secret not in fingerprints[0]


def test_loop_detection_fingerprint_is_canonical_and_argument_sensitive() -> None:
    from assistant_service.core.agent.middlewares.harness import LoopDetectionMiddleware

    first = LoopDetectionMiddleware._fingerprint(
        "search",
        {"query": "x", "filters": {"b": 2, "a": 1}},
    )
    reordered = LoopDetectionMiddleware._fingerprint(
        "search",
        {"filters": {"a": 1, "b": 2}, "query": "x"},
    )
    changed = LoopDetectionMiddleware._fingerprint(
        "search",
        {"query": "y", "filters": {"a": 1, "b": 2}},
    )

    assert first == reordered
    assert first != changed


@pytest.mark.asyncio
async def test_time_budget_middleware_denies_after_budget() -> None:
    from assistant_service.core.agent.middleware import VerdictKind
    from assistant_service.core.agent.middlewares.harness import TimeBudgetMiddleware

    ctx = _Ctx(trace_started_at=time.time() - 10)
    middleware = TimeBudgetMiddleware(max_seconds=1)

    assert (await middleware.on_tool_call(ctx, "search", {})).kind is VerdictKind.DENY


@pytest.mark.asyncio
async def test_call_and_time_limits_reuse_run_budget_truth() -> None:
    from assistant_service.core.agent.middleware import VerdictKind
    from assistant_service.core.agent.middlewares.harness import (
        CallLimitMiddleware,
        TimeBudgetMiddleware,
    )
    from assistant_service.core.run_budget import RunBudget, RunBudgetLimits

    now = [0.0]
    budget = RunBudget(
        limits=RunBudgetLimits(
            max_model_turns=4,
            max_tool_calls=2,
            max_parallel_tool_calls=2,
            max_wall_time_seconds=1.0,
            max_tool_result_bytes=1024,
        ),
        tool_calls=3,
        clock=lambda: now[0],
    )
    ctx = _Ctx()
    ctx.run_budget = budget

    assert (
        await CallLimitMiddleware(max_calls=99).on_tool_call(ctx, "search", {})
    ).kind is VerdictKind.DENY

    now[0] = 2.0
    assert (
        await TimeBudgetMiddleware(max_seconds=99).on_tool_call(ctx, "search", {})
    ).kind is VerdictKind.DENY


@pytest.mark.asyncio
async def test_internal_profile_limits_can_only_tighten_operator_limits() -> None:
    from assistant_service.core.agent.agent_loop_models import (
        AgentLoopConfig,
        AgentReliabilityLimits,
    )
    from assistant_service.core.agent.middleware import VerdictKind
    from assistant_service.core.agent.middlewares.harness import LoopDetectionMiddleware

    ctx = _Ctx()
    ctx.config = AgentLoopConfig(
        execution_profile="safe",
        reliability_limits=AgentReliabilityLimits(max_identical_tool_calls=3),
        reliability_profile_limits={
            "safe": AgentReliabilityLimits(max_identical_tool_calls=2),
        },
    )
    middleware = LoopDetectionMiddleware(max_repeats=99)

    assert (
        await middleware.on_tool_call(ctx, "search", {"query": "x"})
    ).kind is VerdictKind.ALLOW
    assert (
        await middleware.on_tool_call(ctx, "search", {"query": "x"})
    ).kind is VerdictKind.ALLOW
    assert (
        await middleware.on_tool_call(ctx, "search", {"query": "x"})
    ).kind is VerdictKind.DENY

    wider_profile_ctx = _Ctx()
    wider_profile_ctx.config = AgentLoopConfig(
        execution_profile="power",
        reliability_limits=AgentReliabilityLimits(max_identical_tool_calls=3),
        reliability_profile_limits={
            "power": AgentReliabilityLimits(max_identical_tool_calls=99),
        },
    )
    wider_profile_middleware = LoopDetectionMiddleware(max_repeats=99)
    wider_verdicts = [
        await wider_profile_middleware.on_tool_call(
            wider_profile_ctx,
            "search",
            {"query": "x"},
        )
        for _ in range(4)
    ]
    assert [verdict.kind for verdict in wider_verdicts[:3]] == [
        VerdictKind.ALLOW
    ] * 3
    assert wider_verdicts[3].kind is VerdictKind.DENY


def test_internal_call_and_time_limits_tighten_canonical_run_budget() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop
    from assistant_service.core.agent.agent_loop_models import (
        AgentLoopConfig,
        AgentReliabilityLimits,
    )
    from assistant_service.core.run_budget import RunBudgetLimits

    budget = AgentLoop._configured_run_budget(
        AgentLoopConfig(
            execution_profile="safe",
            run_budget_limits=RunBudgetLimits(
                max_model_turns=10,
                max_tool_calls=20,
                max_parallel_tool_calls=4,
                max_wall_time_seconds=30,
                max_tool_result_bytes=1024,
            ),
            reliability_limits=AgentReliabilityLimits(
                max_tool_calls=10,
                max_wall_time_seconds=20,
            ),
            reliability_profile_limits={
                "safe": AgentReliabilityLimits(
                    max_tool_calls=3,
                    max_wall_time_seconds=5,
                ),
            },
        )
    )

    assert budget.limits.max_tool_calls == 3
    assert budget.limits.max_wall_time_seconds == 5

    wider_profile_budget = AgentLoop._configured_run_budget(
        AgentLoopConfig(
            execution_profile="power",
            run_budget_limits=RunBudgetLimits(
                max_model_turns=10,
                max_tool_calls=20,
                max_parallel_tool_calls=4,
                max_wall_time_seconds=30,
                max_tool_result_bytes=1024,
            ),
            reliability_limits=AgentReliabilityLimits(
                max_tool_calls=10,
                max_wall_time_seconds=20,
            ),
            reliability_profile_limits={
                "power": AgentReliabilityLimits(
                    max_tool_calls=99,
                    max_wall_time_seconds=99,
                ),
            },
        )
    )

    assert wider_profile_budget.limits.max_tool_calls == 10
    assert wider_profile_budget.limits.max_wall_time_seconds == 20


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


@pytest.mark.asyncio
async def test_trace_sensor_is_bounded_and_on_error_remains_async_generator() -> None:
    from assistant_service.core.agent.middlewares.harness import TraceSensorMiddleware

    ctx = _Ctx()
    middleware = TraceSensorMiddleware(max_events=2, max_errors=1)

    assert inspect.isasyncgenfunction(middleware.on_error)
    for event_type in ("run_started", "status", "run_finished"):
        await middleware.on_stream_event(ctx, _Event(event_type=event_type, data={}))
    for error in (ValueError("first"), RuntimeError("second")):
        async for _event in middleware.on_error(ctx, error, "generation"):
            pass

    assert ctx._middleware_trace_events == ["status", "run_finished"]
    assert ctx._middleware_trace_errors == [
        {"phase": "generation", "error_type": "RuntimeError"}
    ]
