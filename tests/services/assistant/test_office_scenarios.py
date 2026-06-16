"""
Office scenario detection tests.
"""

from assistant_service.core.office.scenario import OfficeScenario, detect_scenario


def test_detect_meeting_minutes():
    assert detect_scenario("整理会议纪要") == OfficeScenario.MEETING_MINUTES
