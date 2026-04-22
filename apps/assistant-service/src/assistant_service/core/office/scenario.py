"""Office scenario detection for assistant requests."""

from enum import Enum


class OfficeScenario(str, Enum):
    MEETING_MINUTES = "meeting_minutes"
    EMAIL_DRAFT = "email_draft"
    DOC_SUMMARY = "doc_summary"
    GENERIC = "generic"


_RULES = [
    ("会议纪要", OfficeScenario.MEETING_MINUTES),
    ("总结", OfficeScenario.DOC_SUMMARY),
    ("邮件", OfficeScenario.EMAIL_DRAFT),
]


def detect_scenario(text: str) -> OfficeScenario:
    for keyword, scenario in _RULES:
        if keyword in text:
            return scenario
    return OfficeScenario.GENERIC
