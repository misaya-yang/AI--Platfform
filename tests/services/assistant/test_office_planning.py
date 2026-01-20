"""Office workflow planning tests."""

from src.services.assistant.office.planner import build_plan_for_scenario
from src.services.assistant.office.scenario import OfficeScenario


def test_meeting_minutes_plan_has_tasks():
    plan = build_plan_for_scenario(OfficeScenario.MEETING_MINUTES, "整理会议纪要")
    assert plan is not None
    assert len(plan.tasks) >= 2
