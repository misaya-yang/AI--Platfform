"""Shared critic types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class IssueSeverity(str, Enum):
    CRITICAL = "critical"  # must block output
    WARNING = "warning"    # SHOULD fix
    INFO = "info"          # advisory


class IssueCategory(str, Enum):
    PLACEHOLDER = "placeholder"
    OVERFLOW = "overflow"
    OVERLAP = "overlap"
    ALIGNMENT = "alignment"
    CONTRAST = "contrast"
    MISSING_IMAGE = "missing_image"
    AI_TELL = "ai_tell"
    FORMULA_ERROR = "formula_error"
    SCHEMA_ERROR = "schema_error"
    OPEN_ERROR = "open_error"
    OTHER = "other"


@dataclass
class Issue:
    category: IssueCategory
    severity: IssueSeverity
    message: str
    target: Optional[int] = None          # page / slide / row index
    hint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "target": self.target,
            "hint": self.hint,
        }


@dataclass
class CriticReport:
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    rerender_targets: list[int] = field(default_factory=list)
    backend: str = "unknown"
    note: str = ""

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == IssueSeverity.CRITICAL)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "backend": self.backend,
            "note": self.note,
            "critical_count": self.critical_count,
            "issues": [i.to_dict() for i in self.issues],
            "rerender_targets": list(self.rerender_targets),
        }
