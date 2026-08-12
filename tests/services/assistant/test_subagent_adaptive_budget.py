from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.agent.subagent_manager import SubAgentManager
from assistant_service.core.agent.subagent_types import (
    SUBAGENT_DEFAULTS,
    SubAgentAdaptiveBudget,
    SubAgentConfig,
    SubAgentType,
)
from assistant_service.core.run_budget import RunBudget, RunBudgetLimits
from assistant_service.core.tool_invocation_contracts import CapabilityAllowlist
from assistant_service.core.tool_invoker import RegistryToolInvoker, ToolInvocationContext
from assistant_service.core.tools.tool_registry import (
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolRegistry,
    ToolRiskLevel,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class _AdvancingClock(_Clock):
    def __call__(self) -> float:
        return self.now


def _budget() -> SubAgentAdaptiveBudget:
    return SubAgentAdaptiveBudget(
        initial_turns=6,
        initial_tool_calls=10,
        initial_timeout_seconds=120,
        max_turns=12,
        max_tool_calls=22,
        hard_timeout_seconds=600,
        idle_timeout_seconds=90,
        last_progress_at=0,
    )


def test_novel_progress_extends_in_small_steps_without_crossing_hard_ceiling() -> None:
    clock = _Clock()
    budget = _budget()

    for index in range(8):
        assert budget.note_progress(f"source:statute-{index}", now=clock.advance(50)) is True
        assert (
            budget.extend_if_needed(
                turns=budget.effective_turns,
                tool_calls=budget.effective_tool_calls,
                now=clock.now,
            )
            is True
        )
    assert budget.note_progress("source:statute-final", now=clock.advance(50)) is True
    assert (
        budget.extend_if_needed(
            turns=budget.effective_turns,
            tool_calls=budget.effective_tool_calls,
            now=clock.now,
        )
        is False
    )

    assert budget.receipt() == {
        "initial": {
            "max_turns": 6,
            "max_tool_calls": 10,
            "timeout_seconds": 120,
        },
        "effective": {
            "max_turns": 12,
            "max_tool_calls": 22,
            "timeout_seconds": 600,
        },
        "hard_ceiling": {
            "max_turns": 12,
            "max_tool_calls": 22,
            "timeout_seconds": 600,
        },
        "extensions": 8,
        "stop_reason": None,
    }


def test_duplicate_progress_and_failures_do_not_extend_or_reset_idle_timeout() -> None:
    clock = _Clock()
    budget = _budget()

    assert budget.note_progress("filing:10-k:2025", now=clock.advance(40)) is True
    assert budget.note_progress("filing:10-k:2025", now=clock.advance(40)) is False
    assert budget.extensions == 0
    assert budget.timed_out(now=clock.advance(51), started_at=0) is True

    budget.note_failure("read_filing:timeout")
    budget.note_failure("read_filing:timeout")
    assert budget.stop_reason == "consecutive_tool_failures"


def test_distinct_failures_do_not_trigger_repeated_failure_stop() -> None:
    budget = _budget()

    budget.note_failure("read_statute:not-found")
    budget.note_failure("read_filing:timeout")

    assert budget.consecutive_failures == 1
    assert budget.stop_reason is None


def test_one_novel_progress_receipt_funds_only_one_extension() -> None:
    budget = _budget()

    assert budget.note_progress("evidence:a", now=10) is True
    assert budget.extend_if_needed(turns=6, tool_calls=10, now=10) is True
    assert budget.extend_if_needed(turns=8, tool_calls=14, now=11) is False

    assert budget.extensions == 1
    assert budget.consumed_progress == budget.novel_progress == 1


def test_progress_tracker_keeps_only_opaque_digests() -> None:
    budget = _budget()
    evidence = "confidential filing evidence that must not be retained"

    budget.note_progress("a" * 64, now=10)

    assert budget._seen_progress == {"a" * 64}
    assert evidence not in repr(budget)


def test_idle_window_starts_at_the_real_start_time_not_zero() -> None:
    budget = _budget()

    assert budget.timed_out(now=10_080, started_at=10_000) is False
    assert budget.timed_out(now=10_090, started_at=10_000) is True
    assert budget.stop_reason == "idle_timeout"


def test_operation_deadline_uses_nearest_lease_idle_or_hard_limit() -> None:
    budget = _budget()

    assert budget.operation_deadline(started_at=1_000) == 1_090
    budget.note_progress("new-source", now=1_080)
    assert budget.operation_deadline(started_at=1_000) == 1_120

    assert budget.extend_if_needed(turns=6, tool_calls=10, now=1_080) is True
    assert budget.operation_deadline(started_at=1_000) == 1_170


def test_hard_timeout_wins_even_when_progress_keeps_idle_window_alive() -> None:
    clock = _Clock()
    budget = _budget()

    for index in range(7):
        clock.advance(80)
        budget.note_progress(f"legal-source:{index}", now=clock.now)
        assert budget.timed_out(now=clock.now, started_at=0) is False

    assert budget.timed_out(now=clock.advance(40), started_at=0) is True


@pytest.mark.asyncio
async def test_plugin_progress_extension_executes_extra_turn_and_records_receipt() -> None:
    registry = ToolRegistry()
    tool_calls = 0

    async def read_source(request: Any) -> ToolCallResult:
        nonlocal tool_calls
        tool_calls += 1
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=f"novel authoritative source {tool_calls}",
        )

    registry.register(
        ToolDefinition(
            name="read_source",
            description="Read one authoritative source",
            parameters=[],
            category=ToolCategory.RETRIEVAL,
            risk_level=ToolRiskLevel.LOW,
            capability_metadata={"operation_kind": "read"},
        ),
        read_source,
    )

    class ResearchModel:
        _models: dict[str, Any] = {}

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_values: Any):
            self.calls += 1
            if self.calls <= 4:
                yield SimpleNamespace(
                    content="",
                    tool_calls=[
                        {
                            "id": f"source-{self.calls}",
                            "type": "function",
                            "function": {"name": "read_source", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                )
                return
            yield SimpleNamespace(content="evidence-backed conclusion", tool_calls=[])

    parent = ToolInvocationContext(
        session_id="parent-session",
        user_id="parent-user",
        tenant_id="tenant-a",
        request_id="parent-request",
        run_id="parent-run",
        user=SimpleNamespace(
            user_id="parent-user",
            is_authenticated=True,
            roles=[],
            tier="normal",
        ),
        capability_allowlist=CapabilityAllowlist(frozenset({"read_source"})),
    )
    model = ResearchModel()
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=registry,
        tool_invoker=RegistryToolInvoker(tool_registry=registry),
    )
    config = SubAgentConfig(
        agent_type=SubAgentType.EXPLORE,
        prompt="Analyze controlling law and adverse authority.",
        profile_id="community-doublecheck:doublecheck",
        max_turns=2,
        max_tool_calls=2,
        max_tokens=4096,
        timeout_seconds=120,
        idle_timeout_seconds=120,
        adaptive_budget=True,
        allowed_tools=frozenset({"read_source"}),
        allowed_tool_categories=frozenset(),
    )

    events = [
        event
        async for event in manager.spawn(
            config,
            parent_user=parent.user,
            parent_tenant_id=parent.tenant_id,
            parent_invocation_context=parent,
            parent_max_turns=6,
            parent_max_tool_calls=8,
            parent_max_tokens=4096,
            parent_timeout_seconds=600,
        )
    ]

    terminal = next(event for event in events if event["event_type"] == "subagent_finished")
    execution = terminal["data"]["effective_execution"]
    assert terminal["data"]["status"] == "completed"
    assert model.calls == 5
    assert tool_calls == 4
    assert execution["initial_limits"]["max_turns"] == 2
    assert execution["limits"]["max_turns"] == 6
    assert execution["extensions"] == 2
    assert execution["hard_limits"]["max_turns"] == 6


def test_parent_ceiling_remains_hard_for_adaptive_plugin_budget() -> None:
    requested = SubAgentConfig(
        agent_type=SubAgentType.EXPLORE,
        prompt="Review legal sources",
        profile_id="community-doublecheck:doublecheck",
        max_turns=6,
        max_tool_calls=10,
        max_tokens=4096,
        timeout_seconds=120,
        idle_timeout_seconds=120,
        adaptive_budget=True,
    )
    bounded = SubAgentManager._bounded_config(
        requested,
        SUBAGENT_DEFAULTS[SubAgentType.EXPLORE],
        parent_max_turns=4,
        parent_max_tool_calls=5,
        parent_max_tokens=1024,
        parent_timeout_seconds=90,
    )
    budget = SubAgentManager._adaptive_budget(
        requested,
        bounded,
        SUBAGENT_DEFAULTS[SubAgentType.EXPLORE],
        parent_max_turns=4,
        parent_max_tool_calls=5,
        parent_timeout_seconds=90,
        started_at=1_000,
    )

    assert budget.max_turns == 4
    assert budget.max_tool_calls == 5
    assert budget.hard_timeout_seconds == 90
    assert bounded.max_tokens == 1024


def test_builtin_agent_uses_host_initial_lease_and_operator_ceiling() -> None:
    requested = SubAgentConfig(
        agent_type=SubAgentType.EXPLORE,
        prompt="Research controlling law",
        max_turns=16,
        max_tool_calls=32,
        max_tokens=4096,
        timeout_seconds=600,
    )
    bounded = SubAgentManager._bounded_config(
        requested,
        SUBAGENT_DEFAULTS[SubAgentType.EXPLORE],
        parent_max_turns=None,
        parent_max_tool_calls=None,
        parent_max_tokens=None,
        parent_timeout_seconds=None,
    )
    budget = SubAgentManager._adaptive_budget(
        requested,
        bounded,
        SUBAGENT_DEFAULTS[SubAgentType.EXPLORE],
        parent_max_turns=None,
        parent_max_tool_calls=None,
        parent_timeout_seconds=None,
        started_at=1_000,
    )

    assert budget.initial_turns == 8
    assert budget.initial_tool_calls == 15
    assert budget.initial_timeout_seconds == 120
    assert budget.max_turns == 16
    assert budget.max_tool_calls == 32
    assert budget.hard_timeout_seconds == 600

    prompt = SubAgentManager(
        model_registry=SimpleNamespace(_models={}),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )._build_system_prompt(bounded, SUBAGENT_DEFAULTS[SubAgentType.EXPLORE])
    assert "initial execution lease is 8 turns" in prompt
    assert "initial execution lease is 16 turns" not in prompt


@pytest.mark.asyncio
async def test_virtual_clock_idle_timeout_stops_plugin_without_wall_clock_sleep() -> None:
    clock = _AdvancingClock()

    class IdleModel:
        _models: dict[str, Any] = {}

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_values: Any):
            self.calls += 1
            clock.advance(31)
            yield SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "idle-source",
                        "type": "function",
                        "function": {"name": "read_source", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
            )

    model = IdleModel()
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        monotonic=clock,
    )
    events = [
        event
        async for event in manager.spawn(
            SubAgentConfig(
                agent_type=SubAgentType.EXPLORE,
                prompt="Research the statute",
                profile_id="community-doublecheck:doublecheck",
                max_turns=2,
                max_tool_calls=2,
                max_tokens=4096,
                timeout_seconds=60,
                idle_timeout_seconds=30,
                adaptive_budget=True,
                allowed_tools=frozenset(),
                allowed_tool_categories=frozenset(),
            ),
            parent_timeout_seconds=300,
        )
    ]

    terminal = next(event for event in events if event["event_type"] == "subagent_finished")
    assert terminal["data"]["status"] == "failed"
    assert terminal["data"]["error"].startswith("Timeout after")
    assert terminal["data"]["effective_execution"]["stop_reason"] == "idle_timeout"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_child_stops_before_using_parent_terminal_synthesis_headroom() -> None:
    class NeverCalledModel:
        _models: dict[str, Any] = {}

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_values: Any):
            self.calls += 1
            yield SimpleNamespace(content="unexpected", tool_calls=[])

    budget = RunBudget(
        RunBudgetLimits(
            max_model_turns=2,
            max_tool_calls=2,
            max_parallel_tool_calls=1,
            max_wall_time_seconds=60,
            max_tool_result_bytes=10_000,
            final_synthesis_headroom=1,
        )
    )
    budget.consume_model_turn()
    model = NeverCalledModel()
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )

    events = [
        event
        async for event in manager.spawn(
            SubAgentConfig(
                agent_type=SubAgentType.EXPLORE,
                prompt="Research controlling law",
                max_turns=2,
                max_tool_calls=2,
                max_tokens=4096,
                timeout_seconds=120,
                allowed_tools=frozenset(),
                allowed_tool_categories=frozenset(),
            ),
            run_budget=budget,
        )
    ]

    terminal = next(event for event in events if event["event_type"] == "subagent_finished")
    assert terminal["data"]["status"] == "failed"
    assert "reserved terminal synthesis headroom preserved" in terminal["data"]["error"]
    assert (
        terminal["data"]["effective_execution"]["stop_reason"]
        == "parent_model_turn_budget_exhausted"
    )
    assert model.calls == 0
    assert budget.model_turns == 1
    assert budget.exhausted is False

    budget.consume_model_turn(purpose="synthesis")
    assert budget.model_turns == 2
    assert budget.exhausted is False
