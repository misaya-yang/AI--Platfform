"""Office scenario planning helpers."""

from __future__ import annotations

from .scenario import OfficeScenario
from .templates import MEETING_MINUTES_TEMPLATE
from ..task_planner import ExecutionPlan, PlannedTask, TaskType


def build_plan_for_scenario(scenario: OfficeScenario, request: str) -> ExecutionPlan | None:
    if scenario != OfficeScenario.MEETING_MINUTES:
        return None

    tasks = [
        PlannedTask(
            id="extract_key_points",
            type=TaskType.ANALYZE,
            tool="analyze",
            description="Extract key points",
        ),
        PlannedTask(
            id="generate_minutes",
            type=TaskType.GENERATE,
            tool="generate_document",
            description="Generate minutes",
            dependencies={"extract_key_points"},
        ),
    ]

    return ExecutionPlan(
        goal=MEETING_MINUTES_TEMPLATE["goal"],
        tasks=tasks,
        parallel_groups=[["extract_key_points"], ["generate_minutes"]],
    )
