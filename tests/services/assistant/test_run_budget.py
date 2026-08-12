from __future__ import annotations

import pytest
from assistant_service.core.run_budget import (
    RunBudget,
    RunBudgetDimension,
    RunBudgetExceeded,
    RunBudgetLimits,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _limits(**overrides: int | float) -> RunBudgetLimits:
    values: dict[str, int | float] = {
        "max_model_turns": 2,
        "max_tool_calls": 3,
        "max_parallel_tool_calls": 2,
        "max_wall_time_seconds": 10.0,
        "max_tool_result_bytes": 8,
    }
    values.update(overrides)
    return RunBudgetLimits(**values)  # type: ignore[arg-type]


def _valid_snapshot() -> dict[str, object]:
    budget = RunBudget(_limits())
    budget.consume_model_turn()
    budget.reserve_tool_batch(1)
    budget.observe_tool_result("abc")
    return budget.snapshot()


def test_run_budget_enforces_model_tool_parallel_and_utf8_bytes() -> None:
    budget = RunBudget(_limits())

    budget.consume_model_turn()
    budget.consume_model_turn()
    with pytest.raises(RunBudgetExceeded) as model_error:
        budget.consume_model_turn()
    assert model_error.value.dimension is RunBudgetDimension.MODEL_TURNS
    assert model_error.value.reason == "model_turns_exhausted"

    parallel_budget = RunBudget(_limits())
    with pytest.raises(RunBudgetExceeded) as parallel_error:
        parallel_budget.reserve_tool_batch(3)
    assert parallel_error.value.dimension is RunBudgetDimension.PARALLEL_TOOL_CALLS
    assert parallel_budget.tool_calls == 0

    tool_budget = RunBudget(_limits())
    tool_budget.reserve_tool_batch(2)
    with pytest.raises(RunBudgetExceeded) as tool_error:
        tool_budget.reserve_tool_batch(2)
    assert tool_error.value.dimension is RunBudgetDimension.TOOL_CALLS
    assert tool_budget.tool_calls == 2

    result_budget = RunBudget(_limits(max_tool_result_bytes=5))
    assert result_budget.observe_tool_result("你") == 3
    with pytest.raises(RunBudgetExceeded) as result_error:
        result_budget.observe_tool_result("好")
    assert result_error.value.dimension is RunBudgetDimension.TOOL_RESULT_BYTES
    assert result_budget.tool_result_bytes == 3


def test_run_budget_wall_time_is_active_and_hard() -> None:
    clock = FakeClock()
    budget = RunBudget(_limits(max_wall_time_seconds=2.0), clock=clock)

    clock.value += 1.5
    assert budget.remaining_wall_time_seconds == pytest.approx(0.5)
    clock.value += 0.6
    with pytest.raises(RunBudgetExceeded) as error:
        budget.check_wall_time()
    assert error.value.dimension is RunBudgetDimension.WALL_TIME


def test_run_budget_reserves_terminal_synthesis_headroom() -> None:
    budget = RunBudget(_limits(max_model_turns=4, final_synthesis_headroom=2))

    assert budget.remaining_work_model_turns == 2
    budget.consume_model_turn()
    budget.consume_model_turn()
    assert budget.remaining_work_model_turns == 0
    with pytest.raises(RunBudgetExceeded) as error:
        budget.consume_model_turn()
    assert error.value.limit == 2

    # The sticky exhausted signal is deliberate. A well-behaved loop checks
    # headroom before crossing it; a fresh equivalent budget shows that only
    # explicit synthesis may consume the reserved terminal turns.
    synthesis_budget = RunBudget(_limits(max_model_turns=4, final_synthesis_headroom=2))
    synthesis_budget.consume_model_turn()
    synthesis_budget.consume_model_turn()
    synthesis_budget.consume_model_turn(purpose="synthesis")
    synthesis_budget.consume_model_turn(purpose="synthesis")
    with pytest.raises(RunBudgetExceeded):
        synthesis_budget.consume_model_turn(purpose="synthesis")


def test_run_budget_restore_preserves_usage_and_cannot_expand_limits() -> None:
    first_clock = FakeClock()
    original = RunBudget(_limits(), clock=first_clock)
    original.consume_model_turn()
    original.reserve_tool_batch(2)
    original.observe_tool_result("abc")
    first_clock.value += 4.0
    snapshot = original.snapshot()

    resume_clock = FakeClock()
    configured = _limits(
        max_model_turns=9,
        max_tool_calls=9,
        max_parallel_tool_calls=9,
        max_wall_time_seconds=99.0,
        max_tool_result_bytes=99,
    )
    restored = RunBudget.restore(
        configured_limits=configured,
        snapshot=snapshot,
        clock=resume_clock,
    )

    assert restored.limits == original.limits
    assert restored.model_turns == 1
    assert restored.tool_calls == 2
    assert restored.tool_result_bytes == 3
    assert restored.elapsed_seconds == pytest.approx(4.0)
    resume_clock.value += 1.0
    assert restored.elapsed_seconds == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("max_model_turns", True),
        ("max_tool_calls", 1.0),
        ("max_parallel_tool_calls", False),
        ("max_tool_result_bytes", 8.0),
        ("max_wall_time_seconds", True),
        ("max_wall_time_seconds", float("nan")),
        ("max_wall_time_seconds", float("inf")),
        ("max_wall_time_seconds", float("-inf")),
        ("max_wall_time_seconds", 10**400),
    ],
)
def test_run_budget_limits_reject_non_canonical_numbers(
    field_name: str,
    invalid_value: int | float,
) -> None:
    with pytest.raises(ValueError):
        _limits(**{field_name: invalid_value})


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        ("top_level_field_missing", "invalid run budget snapshot fields"),
        ("top_level_field_extra", "invalid run budget snapshot fields"),
        ("unknown_schema", "unsupported run budget snapshot schema"),
        ("malformed_usage", "run budget usage must be an object"),
        ("boolean_counter", "model_turns must be a non-negative integer"),
        ("float_counter", "tool_calls must be a non-negative integer"),
        ("counter_over_limit", "model_turns exceeds persisted limit"),
        ("elapsed_over_wall", "elapsed_ms exceeds persisted wall-time limit"),
        ("remaining_tamper", "run budget remaining counters are inconsistent"),
        ("exhausted_tamper", "exhausted run budget cannot be resumed"),
        ("limit_nan", "max_wall_time_seconds must be positive and finite"),
        ("limit_boolean", "max_model_turns must be a positive integer"),
        ("limit_float", "max_tool_calls must be a positive integer"),
        (
            "headroom_reduction",
            "persisted run budget limits exceed configured limits",
        ),
        ("limit_escalation", "persisted run budget limits exceed configured limits"),
    ],
)
def test_run_budget_restore_rejects_tampered_snapshots(
    case: str,
    expected_message: str,
) -> None:
    snapshot = _valid_snapshot()
    limits = snapshot["limits"]
    usage = snapshot["usage"]
    remaining = snapshot["remaining"]
    assert isinstance(limits, dict)
    assert isinstance(usage, dict)
    assert isinstance(remaining, dict)

    if case == "top_level_field_missing":
        snapshot.pop("reason")
    elif case == "top_level_field_extra":
        snapshot["unexpected"] = True
    elif case == "unknown_schema":
        snapshot["schema_version"] = "assistant-run-budget/v999"
    elif case == "malformed_usage":
        snapshot["usage"] = []
    elif case == "boolean_counter":
        usage["model_turns"] = True
    elif case == "float_counter":
        usage["tool_calls"] = 1.0
    elif case == "counter_over_limit":
        usage["model_turns"] = int(limits["max_model_turns"]) + 1
    elif case == "elapsed_over_wall":
        usage["elapsed_ms"] = int(float(limits["max_wall_time_seconds"]) * 1000) + 1
    elif case == "remaining_tamper":
        remaining["tool_calls"] = int(remaining["tool_calls"]) + 1
    elif case == "exhausted_tamper":
        snapshot["exhausted"] = True
        snapshot["reason"] = "tool_calls_exhausted"
    elif case == "limit_nan":
        limits["max_wall_time_seconds"] = float("nan")
    elif case == "limit_boolean":
        limits["max_model_turns"] = True
    elif case == "limit_float":
        limits["max_tool_calls"] = 3.0
    elif case == "headroom_reduction":
        limits["final_synthesis_headroom"] = 1
        configured = _limits(max_model_turns=4, final_synthesis_headroom=2)
        with pytest.raises(ValueError, match=expected_message):
            RunBudget.restore(configured_limits=configured, snapshot=snapshot)
        return
    elif case == "limit_escalation":
        limits["max_model_turns"] = _limits().max_model_turns + 1
    else:  # pragma: no cover - keeps the case table exhaustive
        raise AssertionError(case)

    with pytest.raises(ValueError, match=expected_message):
        RunBudget.restore(configured_limits=_limits(), snapshot=snapshot)  # type: ignore[arg-type]


@pytest.mark.parametrize("snapshot", [None, [], "invalid"])
def test_run_budget_restore_requires_an_object_snapshot(snapshot: object) -> None:
    with pytest.raises(ValueError, match="run budget snapshot is required"):
        RunBudget.restore(configured_limits=_limits(), snapshot=snapshot)  # type: ignore[arg-type]


def test_run_budget_snapshot_rounds_elapsed_up_across_resume() -> None:
    first_clock = FakeClock()
    original = RunBudget(_limits(), clock=first_clock)
    first_clock.value += 0.0001

    first_snapshot = original.snapshot()
    assert first_snapshot["usage"]["elapsed_ms"] == 1

    resume_clock = FakeClock()
    restored = RunBudget.restore(
        configured_limits=_limits(),
        snapshot=first_snapshot,
        clock=resume_clock,
    )
    second_snapshot = restored.snapshot()

    assert restored.elapsed_seconds >= 0.001
    assert second_snapshot["usage"]["elapsed_ms"] >= first_snapshot["usage"]["elapsed_ms"]


def test_legacy_mapping_is_finite() -> None:
    limits = RunBudgetLimits.from_legacy(
        max_tool_iterations=5,
        max_concurrent_tools=3,
    )

    assert limits.max_model_turns == 7
    assert limits.max_tool_calls == 15
    assert limits.max_parallel_tool_calls == 3
    assert limits.max_wall_time_seconds == 300.0
    assert limits.max_tool_result_bytes == 256_000
