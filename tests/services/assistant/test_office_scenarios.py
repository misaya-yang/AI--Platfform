"""
Office scenario detection tests.
"""

from src.services.assistant.office.scenario import detect_scenario, OfficeScenario


def test_detect_meeting_minutes():
    assert detect_scenario("整理会议纪要") == OfficeScenario.MEETING_MINUTES


def test_office_eval_checklist_exists():
    import os

    assert os.path.exists("docs/plans/office-assistant-eval-checklist.md")
