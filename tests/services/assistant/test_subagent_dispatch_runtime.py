from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.agent.agent_loop import AgentLoop
from assistant_service.core.agent.agent_loop_models import AgentLoopPhase
from assistant_service.core.agent.streaming_state import (
    StreamingLoopResult,
    StreamingToolCallState,
)
from assistant_service.core.agent.streaming_tool_execution import StreamingToolExecutionMixin
from assistant_service.core.agent.subagent_dispatch_runtime import (
    DispatchScope,
    SubAgentConcurrencyExceeded,
    SubAgentConcurrencyLimiter,
    SubAgentCycleDetected,
    SubAgentDepthExceeded,
    SubAgentDispatchCapacityExceeded,
    SubAgentDispatchConflict,
    SubAgentDispatchInFlight,
    SubAgentDispatchRegistry,
    SubAgentDispatchUncertain,
    canonical_sha256,
)
from assistant_service.core.agent.subagent_manager import SubAgentManager
from assistant_service.core.agent.subagent_types import SubAgentConfig, SubAgentType
from assistant_service.core.run_budget import RunBudgetDimension, RunBudgetExceeded
from assistant_service.core.tool_invoker import ToolInvocationContext
from assistant_service.core.tools.subagent_tool import SpawnSubAgentExecutor
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolRegistry,
)


def _request(arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        call_id="parent-call",
        tool_name="spawn_subagent",
        arguments=arguments,
    )


class _CoordinatorMiddleware:
    async def run_on_tool_result(self, *_args: Any) -> ToolCallResult:
        return _args[-1]


class _CoordinatorHarness(StreamingToolExecutionMixin):
    model_registry = object()
    tool_invoker = object()
    execution_gateway = None
    middleware_chain = _CoordinatorMiddleware()

    def __init__(
        self,
        marker: dict[str, Any],
        manager: Any,
        registry: SubAgentDispatchRegistry,
    ) -> None:
        self.marker = marker
        self.manager = manager
        self.registry = registry

    async def _invoke_tool(self, **_kwargs: Any) -> ToolCallResult:
        return ToolCallResult(
            call_id="parent-call",
            tool_name="spawn_subagent",
            success=True,
            result=self.marker,
        )

    def _subagent_dispatch_registry(self) -> SubAgentDispatchRegistry:
        return self.registry

    def _get_subagent_manager(self) -> Any:
        return self.manager

    def _build_invocation_context(self, *_args: Any, **_kwargs: Any) -> object:
        return object()

    _format_subagent_model_result = staticmethod(AgentLoop._format_subagent_model_result)
    _validate_subagent_terminal = staticmethod(AgentLoop._validate_subagent_terminal)
    _side_effect_recovery = staticmethod(AgentLoop._side_effect_recovery)


async def _production_marker(*, batch: bool = False) -> dict[str, Any]:
    arguments: dict[str, Any]
    if batch:
        arguments = {
            "tasks": [
                {
                    "agent_type": "task",
                    "prompt": "bounded child",
                    "description": "bounded child",
                }
            ],
            "max_concurrency": 1,
        }
    else:
        arguments = {
            "agent_type": "task",
            "prompt": "bounded child",
            "description": "bounded child",
        }
    result = await SpawnSubAgentExecutor().execute(_request(arguments))
    assert result.success is True
    return result.result


def _coordinator_stream(
    harness: _CoordinatorHarness,
) -> tuple[Any, StreamingToolCallState]:
    ctx = SimpleNamespace(
        message="delegate",
        config=SimpleNamespace(
            kb_dataset_ids=[],
            max_tool_iterations=5,
            max_concurrent_tools=5,
            max_tokens=1000,
            model_id="test-model",
            queue_mode="collect",
        ),
        tenant_id="tenant-a",
        cancel_event=None,
        attempt_id="attempt-1",
        run_budget=None,
        run_id="run-1",
        session_id="session-1",
        cancelled=False,
        terminal_exit_reason=None,
    )
    state = SimpleNamespace(
        denied_tools=set(),
        contexts_for_persistence=[],
        iteration=1,
    )
    frame = StreamingToolCallState(
        tool_index=0,
        tool_call={},
        tool_calls_batch=[],
        tool_id="parent-call",
        tool_name="spawn_subagent",
        tool_args={},
    )
    stream = harness._invoke_streaming_tool(
        ctx,
        SimpleNamespace(),
        phase=AgentLoopPhase.EXECUTION,
        state=state,  # type: ignore[arg-type]
        frame=frame,
        out=StreamingLoopResult(),
    )
    return stream, frame


def _assert_coordinator_aborted(
    registry: SubAgentDispatchRegistry,
    marker: dict[str, Any],
) -> None:
    with pytest.raises(SubAgentDispatchUncertain):
        registry.begin(
            DispatchScope("tenant-a", "session-1", "run-1"),
            delegation_id=marker["delegation_id"],
            request_sha256=marker["request_sha256"],
        )


@pytest.mark.asyncio
async def test_executor_derives_stable_delegation_and_unique_task_ids() -> None:
    executor = SpawnSubAgentExecutor()
    arguments = {
        "tasks": [
            {"agent_type": "explore", "prompt": "inspect a", "description": "a"},
            {"agent_type": "plan", "prompt": "inspect b", "description": "b"},
        ],
        "max_concurrency": 2,
    }

    first = await executor.execute(_request(arguments))
    second = await executor.execute(_request(arguments))

    assert first.success is second.success is True
    assert first.result["delegation_id"] == second.result["delegation_id"]
    assert first.result["request_sha256"] == second.result["request_sha256"]
    first_configs = [SubAgentConfig.from_marker(value) for value in first.result["configs"]]
    assert len({config.task_id for config in first_configs}) == 2
    assert all(config.delegation_id == first.result["delegation_id"] for config in first_configs)


@pytest.mark.asyncio
async def test_executor_rejects_duplicate_explicit_task_ids() -> None:
    executor = SpawnSubAgentExecutor()
    result = await executor.execute(
        _request(
            {
                "delegation_id": "delegation-explicit",
                "tasks": [
                    {
                        "agent_type": "explore",
                        "prompt": "a",
                        "description": "a",
                        "task_id": "duplicate",
                    },
                    {
                        "agent_type": "plan",
                        "prompt": "b",
                        "description": "b",
                        "task_id": "duplicate",
                    },
                ],
            }
        )
    )

    assert result.success is False
    assert "unique" in str(result.error)


def test_dispatch_registry_reuses_complete_and_rejects_conflict_or_inflight() -> None:
    registry = SubAgentDispatchRegistry()
    scope = DispatchScope("tenant-a", "session-a")
    digest = canonical_sha256({"task": "a"})

    assert registry.begin(scope, delegation_id="d1", request_sha256=digest).action == "start"
    with pytest.raises(SubAgentDispatchInFlight):
        registry.begin(scope, delegation_id="d1", request_sha256=digest)
    with pytest.raises(SubAgentDispatchConflict):
        registry.begin(
            scope,
            delegation_id="d1",
            request_sha256=canonical_sha256({"task": "different"}),
        )

    receipt = {"status": "completed", "result": {"claims": ["done"]}}
    registry.complete(
        scope,
        delegation_id="d1",
        request_sha256=digest,
        receipt=receipt,
    )
    reused = registry.begin(scope, delegation_id="d1", request_sha256=digest)
    assert reused.action == "reuse"
    assert reused.receipt == receipt
    reused.receipt["status"] = "tampered"  # type: ignore[index]
    assert registry.begin(scope, delegation_id="d1", request_sha256=digest).receipt == receipt


def test_dispatch_registry_never_reuses_a_result_across_parent_runs() -> None:
    registry = SubAgentDispatchRegistry()
    first_run = DispatchScope("tenant-a", "session-a", "run-a")
    later_run = DispatchScope("tenant-a", "session-a", "run-b")
    digest = canonical_sha256({"task": "same prompt, new run"})

    registry.begin(first_run, delegation_id="d1", request_sha256=digest)
    registry.complete(
        first_run,
        delegation_id="d1",
        request_sha256=digest,
        receipt={"status": "completed", "result_summary": "old answer"},
    )

    assert registry.begin(later_run, delegation_id="d1", request_sha256=digest).action == "start"


def test_dispatch_registry_bounds_inflight_and_uncertain_by_ttl_and_capacity() -> None:
    now = [0.0]
    registry = SubAgentDispatchRegistry(
        max_completed=1,
        max_records=2,
        inflight_ttl_seconds=5,
        uncertain_ttl_seconds=7,
        completed_ttl_seconds=30,
        clock=lambda: now[0],
    )
    scope = DispatchScope("tenant-a", "session-a", "run-a")
    first_digest = canonical_sha256({"task": "first"})
    second_digest = canonical_sha256({"task": "second"})
    third_digest = canonical_sha256({"task": "third"})

    registry.begin(scope, delegation_id="first", request_sha256=first_digest)
    registry.begin(scope, delegation_id="second", request_sha256=second_digest)
    with pytest.raises(SubAgentDispatchCapacityExceeded):
        registry.begin(scope, delegation_id="third", request_sha256=third_digest)

    now[0] = 6
    with pytest.raises(SubAgentDispatchUncertain):
        registry.begin(scope, delegation_id="first", request_sha256=first_digest)
    with pytest.raises(SubAgentDispatchCapacityExceeded):
        registry.begin(scope, delegation_id="third", request_sha256=third_digest)

    now[0] = 14
    assert (
        registry.begin(scope, delegation_id="third", request_sha256=third_digest).action
        == "start"
    )


def test_unknown_side_effect_abort_cannot_be_promoted_or_reused() -> None:
    registry = SubAgentDispatchRegistry()
    scope = DispatchScope("tenant-a", "session-a", "run-a")
    digest = canonical_sha256({"task": "write"})
    registry.begin(scope, delegation_id="write", request_sha256=digest)

    registry.abort(
        scope,
        delegation_id="write",
        request_sha256=digest,
        reason="side_effect_unknown",
        side_effect_unknown=True,
    )

    with pytest.raises(SubAgentDispatchUncertain):
        registry.complete(
            scope,
            delegation_id="write",
            request_sha256=digest,
            receipt={"status": "completed"},
        )
    with pytest.raises(SubAgentDispatchUncertain):
        registry.begin(scope, delegation_id="write", request_sha256=digest)


def test_cached_receipt_rebinds_attempt_without_mutating_recorded_evidence() -> None:
    receipt = {
        "attempt_id": "attempt-old",
        "result": {"status": "completed", "attempt_id": "attempt-old"},
        "results": [
            {"result": {"status": "completed", "attempt_id": "attempt-old"}}
        ],
    }

    rebound = StreamingToolExecutionMixin._rebind_cached_subagent_receipt(
        receipt,
        attempt_id="attempt-new",
    )

    assert receipt["attempt_id"] == "attempt-old"
    assert rebound["attempt_id"] == "attempt-new"
    assert rebound["reused_from_attempt_id"] == "attempt-old"
    assert rebound["result"]["attempt_id"] == "attempt-new"
    assert rebound["results"][0]["result"]["attempt_id"] == "attempt-new"


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_streaming_parse_failure_aborts_claim(batch: bool) -> None:
    marker = await _production_marker(batch=batch)
    if batch:
        marker["configs"] = [{}]
    else:
        marker["config"] = {}
    registry = SubAgentDispatchRegistry()
    harness = _CoordinatorHarness(marker, None, registry)
    stream, _ = _coordinator_stream(harness)

    with pytest.raises(ValueError, match="invalid agent_type"):
        _ = [event async for event in stream]

    _assert_coordinator_aborted(registry, marker)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["spawn", "budget"])
async def test_streaming_spawn_or_budget_failure_aborts_claim(failure_kind: str) -> None:
    marker = await _production_marker()
    error: BaseException
    if failure_kind == "budget":
        error = RunBudgetExceeded(
            dimension=RunBudgetDimension.PARALLEL_TOOL_CALLS,
            limit=1,
            used=1,
            requested=2,
            snapshot={},
        )
    else:
        error = RuntimeError("spawn failed")

    class RaisingManager:
        async def spawn(self, *_args: Any, **_kwargs: Any):
            if error is not None:
                raise error
            yield {}

    registry = SubAgentDispatchRegistry()
    harness = _CoordinatorHarness(marker, RaisingManager(), registry)
    stream, _ = _coordinator_stream(harness)

    with pytest.raises(type(error)):
        _ = [event async for event in stream]

    _assert_coordinator_aborted(registry, marker)


@pytest.mark.asyncio
async def test_streaming_cancellation_aborts_claim() -> None:
    marker = await _production_marker()
    entered = asyncio.Event()

    class WaitingManager:
        async def spawn(self, *_args: Any, **_kwargs: Any):
            entered.set()
            await asyncio.Event().wait()
            yield {}

    registry = SubAgentDispatchRegistry()
    harness = _CoordinatorHarness(marker, WaitingManager(), registry)
    stream, _ = _coordinator_stream(harness)

    async def consume() -> None:
        _ = [event async for event in stream]

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    _assert_coordinator_aborted(registry, marker)


@pytest.mark.asyncio
async def test_streaming_consumer_close_aborts_claim() -> None:
    marker = await _production_marker()

    class StartedManager:
        async def spawn(self, *_args: Any, **_kwargs: Any):
            yield {
                "event_type": "subagent_started",
                "data": {"agent_id": "child", "attempt_id": "attempt-1"},
            }
            await asyncio.Event().wait()

    registry = SubAgentDispatchRegistry()
    harness = _CoordinatorHarness(marker, StartedManager(), registry)
    stream, _ = _coordinator_stream(harness)

    first = await stream.__anext__()
    assert first.event_type == "subagent_started"
    await stream.aclose()

    _assert_coordinator_aborted(registry, marker)


@pytest.mark.asyncio
async def test_streaming_unknown_side_effect_is_terminally_quarantined() -> None:
    marker = await _production_marker()

    class UnknownManager:
        async def spawn(self, *_args: Any, **_kwargs: Any):
            yield {
                "event_type": "subagent_side_effect_unknown",
                "data": {
                    "operation_id": "write-1",
                    "failure": {"failure_kind": "side_effect_unknown"},
                },
            }
            yield {
                "event_type": "subagent_finished",
                "data": {
                    "agent_id": "child",
                    "attempt_id": "attempt-1",
                    "status": "blocked",
                    "result_summary": "",
                    "result": {
                        "status": "blocked",
                        "attempt_id": "attempt-1",
                        "claims": [],
                        "evidence": [],
                        "limitations": ["side effect unknown"],
                    },
                    "recovery": {
                        "operation_id": "write-1",
                        "failure": {"failure_kind": "side_effect_unknown"},
                    },
                },
            }

    registry = SubAgentDispatchRegistry()
    harness = _CoordinatorHarness(marker, UnknownManager(), registry)
    stream, frame = _coordinator_stream(harness)

    _ = [event async for event in stream]

    assert frame.tool_error == "SIDE_EFFECT_UNKNOWN"
    _assert_coordinator_aborted(registry, marker)
    with pytest.raises(SubAgentDispatchUncertain):
        registry.complete(
            DispatchScope("tenant-a", "session-1", "run-1"),
            delegation_id=marker["delegation_id"],
            request_sha256=marker["request_sha256"],
            receipt={"status": "completed"},
        )


def test_concurrency_limiter_is_atomic_across_tenant_and_session() -> None:
    limiter = SubAgentConcurrencyLimiter(tenant_limit=3, session_limit=2)
    first = limiter.acquire(DispatchScope("tenant-a", "session-a"), 2)
    with pytest.raises(SubAgentConcurrencyExceeded, match="session"):
        limiter.acquire(DispatchScope("tenant-a", "session-a"), 1)
    second = limiter.acquire(DispatchScope("tenant-a", "session-b"), 1)
    with pytest.raises(SubAgentConcurrencyExceeded, match="tenant"):
        limiter.acquire(DispatchScope("tenant-a", "session-c"), 1)
    first.release()
    second.release()
    lease = limiter.acquire(DispatchScope("tenant-a", "session-a"), 2)
    lease.release()


def _parent_context(metadata: dict[str, Any] | None = None) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="session-a",
        user_id="user-a",
        tenant_id="tenant-a",
        request_id="request-a",
        run_id="run-a",
        parent_task_id="parent-task",
        metadata=metadata or {},
    )


def test_lineage_default_depth_operator_cap_and_cycle_guard() -> None:
    config = SubAgentConfig(
        agent_type=SubAgentType.TASK,
        prompt="work",
        delegation_id="delegation-a",
        task_id="child-task",
        # Forged model marker values must be ignored.
        depth=99,
        lineage=("forged",),
    )
    bound = SubAgentManager._bind_lineage(config, _parent_context())
    assert bound.depth == 1
    assert bound.parent_task_id == "parent-task"
    assert bound.lineage == ("parent-task",)

    generated_first = SubAgentManager._bind_lineage(
        SubAgentConfig(agent_type=SubAgentType.TASK, prompt="stable work"),
        _parent_context(),
    )
    generated_second = SubAgentManager._bind_lineage(
        SubAgentConfig(agent_type=SubAgentType.TASK, prompt="stable work"),
        _parent_context(),
    )
    assert generated_first.task_id == generated_second.task_id
    assert generated_first.delegation_id == generated_second.delegation_id

    with pytest.raises(SubAgentDepthExceeded):
        SubAgentManager._bind_lineage(
            config,
            _parent_context(
                {
                    "subagent_depth": 1,
                    "subagent_max_depth": 1,
                    "subagent_task_id": "parent-task",
                }
            ),
        )
    with pytest.raises(SubAgentDepthExceeded):
        SubAgentManager._bind_lineage(
            config,
            _parent_context({"subagent_max_depth": 0}),
        )
    depth_two = SubAgentManager._bind_lineage(
        config,
        _parent_context(
            {
                "subagent_depth": 1,
                "subagent_max_depth": 99,
                "subagent_task_id": "parent-task",
            }
        ),
    )
    assert depth_two.depth == 2
    with pytest.raises(SubAgentCycleDetected):
        SubAgentManager._bind_lineage(
            config,
            _parent_context(
                {
                    "subagent_depth": 0,
                    "subagent_lineage": ["child-task"],
                }
            ),
        )


@pytest.mark.asyncio
async def test_terminal_receipt_contains_effective_execution_and_lineage() -> None:
    class Model:
        _models: dict[str, Any] = {}

        async def chat_stream(self, **values: Any):
            assert values["model_id"] == "parent-model"
            yield SimpleNamespace(content="done", tool_calls=[])

    manager = SubAgentManager(
        model_registry=Model(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )
    events = [
        event
        async for event in manager.spawn(
            SubAgentConfig(
                agent_type=SubAgentType.TASK,
                prompt="work",
                delegation_id="delegation-a",
                task_id="task-a",
                max_turns=99,
                max_tool_calls=99,
                max_tokens=99,
                timeout_seconds=99,
            ),
            parent_invocation_context=_parent_context(),
            parent_model_id="parent-model",
            parent_max_turns=2,
            parent_max_tool_calls=3,
            parent_max_tokens=64,
            parent_timeout_seconds=5,
        )
    ]
    terminal = next(event["data"] for event in events if event["event_type"] == "subagent_finished")

    assert terminal["delegation_id"] == "delegation-a"
    assert terminal["task_id"] == "task-a"
    assert terminal["parent_task_id"] == "parent-task"
    assert terminal["depth"] == 1
    assert terminal["lineage"] == ["parent-task"]
    effective = terminal["effective_execution"]
    assert effective["model_id"] == "parent-model"
    assert effective["tool_names"] == []
    assert effective["limits"] == {
        "max_turns": 2,
        "max_tool_calls": 3,
        "max_tokens": 64,
        "timeout_seconds": 5,
    }
    assert effective["usage"]["turns"] == 1
    assert effective["usage"]["tool_calls"] == 0


@pytest.mark.asyncio
async def test_parallel_width_check_does_not_charge_child_tool_count() -> None:
    class Budget:
        checked: list[int] = []
        limits = SimpleNamespace(max_parallel_tool_calls=5)

        def check_parallel_width(self, count: int) -> None:
            self.checked.append(count)

    class Manager(SubAgentManager):
        async def spawn(self, config: SubAgentConfig, *_args: Any, **_kwargs: Any):
            yield {
                "event_type": "subagent_started",
                "data": {"dispatch_index": config.dispatch_index, "agent_id": config.task_id},
            }
            yield {
                "event_type": "subagent_finished",
                "data": {"dispatch_index": config.dispatch_index, "agent_id": config.task_id},
            }

    manager = Manager.__new__(Manager)
    configs = [
        SubAgentConfig(
            agent_type=SubAgentType.TASK,
            prompt=str(index),
            task_id=f"task-{index}",
        )
        for index in range(2)
    ]
    budget = Budget()

    _ = [event async for event in manager.spawn_parallel(configs, run_budget=budget)]  # type: ignore[arg-type]

    assert budget.checked == [2]
