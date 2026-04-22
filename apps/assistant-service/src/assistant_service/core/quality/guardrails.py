"""
Quality Guardrails System for Enterprise Agent.

This module implements hardcoded quality guardrails that define boundaries
for AI-generated content. The Agent operates freely within these boundaries
but must pass validation before final output.

Key Features:
- Quality thresholds (min words, min sections)
- Banned phrase detection
- Tool usage constraints
- Validation result reporting

Reference: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class IssueSeverity(str, Enum):
    """Severity levels for quality issues."""

    CRITICAL = "critical"  # Must be fixed, blocks output
    WARNING = "warning"  # Should be fixed, doesn't block
    INFO = "info"  # Informational only


class DocumentType(str, Enum):
    """Document types with specific quality requirements."""

    PPT = "ppt"
    PPTX = "pptx"
    DOC = "doc"
    DOCX = "docx"
    XLS = "xls"
    XLSX = "xlsx"
    MARKDOWN = "markdown"
    TEXT = "text"


@dataclass
class QualityIssue:
    """
    Represents a quality issue found during validation.

    Attributes:
        type: Category of the issue (e.g., insufficient_content, vague_expression)
        message: Human-readable description of the issue
        severity: How serious the issue is
        action: Suggested action to fix the issue
        location: Optional location info (line number, section, etc.)
    """

    type: str
    message: str
    severity: IssueSeverity
    action: str
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "type": self.type,
            "message": self.message,
            "severity": self.severity.value,
            "action": self.action,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityIssue:
        """Deserialize from dictionary."""
        return cls(
            type=data["type"],
            message=data["message"],
            severity=IssueSeverity(data["severity"]),
            action=data["action"],
            location=data.get("location"),
        )


@dataclass
class ValidationResult:
    """
    Result of quality validation.

    Attributes:
        passed: Whether the content passed validation (no critical issues)
        issues: List of all issues found
        score: Quality score from 0.0 to 1.0
        validated_at: Timestamp of validation
    """

    passed: bool
    issues: list[QualityIssue]
    score: float
    validated_at: datetime = field(default_factory=datetime.now)

    def has_critical_issues(self) -> bool:
        """Check if there are any critical issues."""
        return any(i.severity == IssueSeverity.CRITICAL for i in self.issues)

    def has_warnings(self) -> bool:
        """Check if there are any warnings."""
        return any(i.severity == IssueSeverity.WARNING for i in self.issues)

    def get_issues_by_severity(self, severity: IssueSeverity) -> list[QualityIssue]:
        """Get all issues of a specific severity."""
        return [i for i in self.issues if i.severity == severity]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "score": self.score,
            "validated_at": self.validated_at.isoformat(),
        }


# =============================================================================
# Hardcoded Quality Thresholds (Guardrails)
# =============================================================================

QUALITY_THRESHOLDS: dict[DocumentType, dict[str, Any]] = {
    DocumentType.PPT: {
        "min_slides": 10,
        "min_words_total": 1500,
        "min_words_per_slide": 100,
        "required_pages": ["title", "toc", "summary"],
    },
    DocumentType.PPTX: {
        "min_slides": 10,
        "min_words_total": 1500,
        "min_words_per_slide": 100,
        "required_pages": ["title", "toc", "summary"],
    },
    DocumentType.DOC: {
        "min_words": 2500,
        "min_sections": 6,
        "min_words_per_section": 400,
    },
    DocumentType.DOCX: {
        "min_words": 2500,
        "min_sections": 6,
        "min_words_per_section": 400,
    },
    DocumentType.XLS: {
        "min_rows": 15,
        "require_headers": True,
    },
    DocumentType.XLSX: {
        "min_rows": 15,
        "require_headers": True,
    },
    DocumentType.MARKDOWN: {
        "min_words": 1500,
        "min_sections": 5,
    },
    DocumentType.TEXT: {
        "min_words": 800,
    },
}


# =============================================================================
# Banned Phrases (Guardrails)
# =============================================================================

BANNED_PHRASES: list[str] = [
    # Chinese vague expressions
    "等等",
    "诸如此类",
    "等内容",
    "等方面",
    "等问题",
    "此处省略",
    "详见附录",
    "不再赘述",
    "以此类推",
    "如此等等",
    # Ellipsis patterns
    "...略",
    "……略",
    "...",  # Will match when used as content filler
    "……",
    # English vague expressions
    "etc.",
    "and so on",
    "and more",
    "to be continued",
    "details omitted",
]


# =============================================================================
# Tool Constraints (Guardrails)
# =============================================================================

TOOL_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "generate_document": {
        "requires_content_first": True,
        "min_content_length": 500,
        "description": "Must have substantial content before generating document",
    },
    "generate_ppt": {
        "requires_outline_first": True,
        "min_slides": 8,
        "description": "Must have outline and minimum slides before generating",
    },
    "generate_excel": {
        "min_rows": 10,
        "require_headers": True,
        "description": "Must have minimum data rows with headers",
    },
}


@dataclass
class ToolCallValidation:
    """Result of tool call constraint validation."""

    allowed: bool
    reason: str
    suggestions: list[str] = field(default_factory=list)


class ToolConstraintValidator:
    """
    Validates tool calls against hardcoded constraints.

    Ensures tools are called appropriately (e.g., document generation
    only happens after sufficient content is generated).
    """

    def __init__(
        self,
        custom_constraints: dict[str, dict[str, Any]] | None = None,
    ):
        """Initialize with optional custom constraints."""
        self.constraints = {**TOOL_CONSTRAINTS}
        if custom_constraints:
            self.constraints.update(custom_constraints)

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallValidation:
        """
        Validate a tool call against constraints.

        Args:
            tool_name: Name of the tool being called
            arguments: Arguments passed to the tool
            context: Current execution context

        Returns:
            ToolCallValidation with allowed status and reason
        """
        constraints = self.constraints.get(tool_name, {})

        if not constraints:
            return ToolCallValidation(
                allowed=True,
                reason="No constraints for this tool",
            )

        # Check content requirement
        if constraints.get("requires_content_first"):
            min_length = constraints.get("min_content_length", 500)
            content_length = context.get("generated_content_length", 0)

            # Also check the content in arguments
            arg_content = arguments.get("content", "")
            if isinstance(arg_content, str):
                content_length = max(content_length, len(arg_content))

            if content_length < min_length:
                return ToolCallValidation(
                    allowed=False,
                    reason=f"Insufficient content: {content_length} < {min_length} required",
                    suggestions=[
                        "Generate more content before calling this tool",
                        f"Minimum {min_length} characters required",
                    ],
                )

        # Check outline requirement
        if constraints.get("requires_outline_first") and not context.get("has_outline"):
            return ToolCallValidation(
                allowed=False,
                reason="Outline required before generating presentation",
                suggestions=["Create an outline first"],
            )

        # Check minimum slides
        min_slides = constraints.get("min_slides", 0)
        if min_slides > 0:
            slides = arguments.get("slides", [])
            if len(slides) < min_slides:
                return ToolCallValidation(
                    allowed=False,
                    reason=f"Insufficient slides: {len(slides)} < {min_slides} required",
                    suggestions=[f"Add at least {min_slides - len(slides)} more slides"],
                )

        return ToolCallValidation(
            allowed=True,
            reason="All constraints satisfied",
        )


class QualityGuardrails:
    """
    Hardcoded quality guardrails validator.

    This class validates content against predefined quality thresholds.
    It does NOT use LLM for validation - all checks are deterministic.

    Usage:
        validator = QualityGuardrails()
        result = validator.validate(content, DocumentType.DOCX)

        if not result.passed:
            # Handle issues
            for issue in result.issues:
                print(f"{issue.severity}: {issue.message}")
    """

    def __init__(
        self,
        custom_thresholds: dict[DocumentType, dict[str, Any]] | None = None,
        custom_banned_phrases: list[str] | None = None,
    ):
        """
        Initialize the guardrails validator.

        Args:
            custom_thresholds: Override default thresholds
            custom_banned_phrases: Additional banned phrases
        """
        self.thresholds = {**QUALITY_THRESHOLDS}
        if custom_thresholds:
            for doc_type, overrides in custom_thresholds.items():
                if doc_type in self.thresholds:
                    self.thresholds[doc_type].update(overrides)
                else:
                    self.thresholds[doc_type] = overrides

        self.banned_phrases = list(BANNED_PHRASES)
        if custom_banned_phrases:
            self.banned_phrases.extend(custom_banned_phrases)

    def validate(
        self,
        content: str,
        doc_type: DocumentType,
    ) -> ValidationResult:
        """
        Validate content against guardrails.

        Args:
            content: The content to validate
            doc_type: Type of document being validated

        Returns:
            ValidationResult with pass/fail status and issues
        """
        issues: list[QualityIssue] = []

        # Get thresholds for this document type
        thresholds = self.thresholds.get(doc_type, {})

        # Check word count
        word_count = self._count_words(content)
        min_words = thresholds.get("min_words", thresholds.get("min_words_total", 0))

        if min_words > 0 and word_count < min_words:
            issues.append(
                QualityIssue(
                    type="insufficient_content",
                    message=f"内容不足：{word_count}字 < {min_words}字最低要求",
                    severity=IssueSeverity.CRITICAL,
                    action="expand_content",
                )
            )

        # Check section count
        min_sections = thresholds.get("min_sections", 0)
        if min_sections > 0:
            section_count = self._count_sections(content)
            if section_count < min_sections:
                issues.append(
                    QualityIssue(
                        type="insufficient_sections",
                        message=f"章节不足：{section_count}个 < {min_sections}个最低要求",
                        severity=IssueSeverity.CRITICAL,
                        action="add_sections",
                    )
                )

        # Check banned phrases
        for phrase in self.banned_phrases:
            if phrase in content:
                issues.append(
                    QualityIssue(
                        type="vague_expression",
                        message=f"发现模糊表达：'{phrase}'",
                        severity=IssueSeverity.WARNING,
                        action="replace_vague",
                    )
                )

        # Calculate score
        score = self._calculate_score(content, thresholds, issues)

        # Determine if passed (no critical issues)
        passed = not any(i.severity == IssueSeverity.CRITICAL for i in issues)

        return ValidationResult(
            passed=passed,
            issues=issues,
            score=score,
        )

    def _count_words(self, content: str) -> int:
        """
        Count words in content.

        Handles both Chinese (character-based) and English (space-separated).
        """
        # Remove markdown formatting
        clean = re.sub(r"[#*`~\[\](){}|]", "", content)
        # Remove extra whitespace
        clean = re.sub(r"\s+", " ", clean).strip()

        # For Chinese, count characters (excluding spaces and punctuation)
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", clean))
        # For English, count words
        english_words = len(re.findall(r"[a-zA-Z]+", clean))

        return chinese_chars + english_words

    def _count_sections(self, content: str) -> int:
        """Count sections in markdown content."""
        # Count markdown headers (# ## ### etc.)
        headers = re.findall(r"^#{1,6}\s+.+$", content, re.MULTILINE)
        return len(headers)

    def _calculate_score(
        self,
        content: str,
        thresholds: dict[str, Any],
        issues: list[QualityIssue],
    ) -> float:
        """
        Calculate quality score from 0.0 to 1.0.

        Factors:
        - Word count ratio (0.4 weight)
        - Section count ratio (0.3 weight)
        - Issue penalty (0.3 weight)
        """
        score = 1.0

        # Word count factor
        min_words = thresholds.get("min_words", thresholds.get("min_words_total", 0))
        if min_words > 0:
            word_count = self._count_words(content)
            word_ratio = min(word_count / min_words, 1.0)
            score *= 0.6 + 0.4 * word_ratio

        # Issue penalty
        critical_count = sum(1 for i in issues if i.severity == IssueSeverity.CRITICAL)
        warning_count = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)

        score -= critical_count * 0.2
        score -= warning_count * 0.05

        return max(0.0, min(1.0, score))
