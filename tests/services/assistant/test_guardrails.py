"""Tests for the guardrails module."""

from __future__ import annotations

import pytest

from src.services.assistant.guardrails import (
    QualityIssue,
    IssueSeverity,
    ValidationResult,
    DocumentType,
    QUALITY_THRESHOLDS,
    BANNED_PHRASES,
    TOOL_CONSTRAINTS,
    QualityGuardrails,
    ToolConstraintValidator,
)


class TestQualityIssue:
    """Tests for QualityIssue dataclass."""

    def test_create_quality_issue(self):
        """Test creating a QualityIssue instance."""
        issue = QualityIssue(
            type="insufficient_content",
            message="Content is too short: 100 words < 500 words",
            severity=IssueSeverity.CRITICAL,
            action="expand_content",
        )

        assert issue.type == "insufficient_content"
        assert issue.severity == IssueSeverity.CRITICAL
        assert issue.action == "expand_content"
        assert "100 words" in issue.message

    def test_quality_issue_to_dict(self):
        """Test serialization of QualityIssue."""
        issue = QualityIssue(
            type="vague_expression",
            message="Found vague phrase: '等等'",
            severity=IssueSeverity.WARNING,
            action="replace_vague",
        )

        data = issue.to_dict()

        assert data["type"] == "vague_expression"
        assert data["severity"] == "warning"
        assert data["action"] == "replace_vague"


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_validation_passed(self):
        """Test validation result when all checks pass."""
        result = ValidationResult(
            passed=True,
            issues=[],
            score=1.0,
        )

        assert result.passed is True
        assert len(result.issues) == 0
        assert result.score == 1.0

    def test_validation_failed_with_critical_issue(self):
        """Test validation result with critical issues."""
        issues = [
            QualityIssue(
                type="insufficient_content",
                message="Content too short",
                severity=IssueSeverity.CRITICAL,
                action="expand_content",
            )
        ]

        result = ValidationResult(
            passed=False,
            issues=issues,
            score=0.3,
        )

        assert result.passed is False
        assert len(result.issues) == 1
        assert result.has_critical_issues() is True

    def test_validation_with_only_warnings(self):
        """Test validation result with only warnings."""
        issues = [
            QualityIssue(
                type="vague_expression",
                message="Found '等等'",
                severity=IssueSeverity.WARNING,
                action="replace_vague",
            )
        ]

        result = ValidationResult(
            passed=True,  # Warnings don't block
            issues=issues,
            score=0.8,
        )

        assert result.passed is True
        assert result.has_critical_issues() is False
        assert result.has_warnings() is True


class TestQualityThresholds:
    """Tests for hardcoded quality thresholds."""

    def test_ppt_thresholds_exist(self):
        """Test PPT has required thresholds."""
        ppt = QUALITY_THRESHOLDS[DocumentType.PPT]

        assert ppt["min_slides"] >= 8
        assert ppt["min_words_total"] >= 600
        assert "required_pages" in ppt

    def test_docx_thresholds_exist(self):
        """Test DOCX has required thresholds."""
        docx = QUALITY_THRESHOLDS[DocumentType.DOCX]

        assert docx["min_words"] >= 1000
        assert docx["min_sections"] >= 4

    def test_xlsx_thresholds_exist(self):
        """Test XLSX has required thresholds."""
        xlsx = QUALITY_THRESHOLDS[DocumentType.XLSX]

        assert xlsx["min_rows"] >= 10
        assert xlsx["require_headers"] is True


class TestBannedPhrases:
    """Tests for banned phrases list."""

    def test_banned_phrases_exist(self):
        """Test banned phrases list is defined."""
        assert isinstance(BANNED_PHRASES, list)
        assert len(BANNED_PHRASES) > 0

    def test_common_vague_phrases_banned(self):
        """Test common vague phrases are in the list."""
        assert "等等" in BANNED_PHRASES
        assert "诸如此类" in BANNED_PHRASES
        assert "等内容" in BANNED_PHRASES

    def test_ellipsis_banned(self):
        """Test ellipsis patterns are banned."""
        assert "...略" in BANNED_PHRASES or "..." in BANNED_PHRASES


class TestQualityGuardrails:
    """Tests for QualityGuardrails validator."""

    def test_validate_sufficient_content(self):
        """Test validation passes for sufficient content."""
        validator = QualityGuardrails()
        # Content with enough words AND sections
        content = """
# 第一章 引言
这是第一章的内容，包含了足够的字数来满足测试要求。

# 第二章 背景
这是第二章的内容，也包含了足够的字数来满足测试要求。

# 第三章 方法
这是第三章的内容，同样包含了足够的字数来满足测试要求。

# 第四章 结论
这是第四章的内容，最后一章也包含了足够的字数来满足测试要求。
""" * 30  # Repeat to meet word count (1000+)

        result = validator.validate(content, DocumentType.DOCX)

        assert result.passed is True
        assert not result.has_critical_issues()

    def test_validate_insufficient_content(self):
        """Test validation fails for insufficient content."""
        validator = QualityGuardrails()
        content = "这是一个很短的内容。"  # ~10 chars

        result = validator.validate(content, DocumentType.DOCX)

        assert result.passed is False
        assert result.has_critical_issues()

        # Check specific issue
        critical = result.get_issues_by_severity(IssueSeverity.CRITICAL)
        assert any(i.type == "insufficient_content" for i in critical)

    def test_validate_banned_phrase_detection(self):
        """Test banned phrases are detected."""
        validator = QualityGuardrails()
        content = "这个功能包括很多方面，等等。" * 100

        result = validator.validate(content, DocumentType.DOCX)

        # Banned phrase should be a warning
        assert result.has_warnings()
        warnings = result.get_issues_by_severity(IssueSeverity.WARNING)
        assert any(i.type == "vague_expression" for i in warnings)

    def test_validate_section_count(self):
        """Test section count validation for documents."""
        validator = QualityGuardrails()
        # Content with only 2 sections (needs 4) but enough words
        # Using plain text without headers but enough words
        base_content = "这是一段很长的内容用于测试字数统计功能" * 100
        content = f"""
# 第一章
{base_content}

# 第二章
{base_content}
"""
        result = validator.validate(content, DocumentType.DOCX)

        # Should have insufficient sections issue (2 < 4)
        issues = [i for i in result.issues if i.type == "insufficient_sections"]
        assert len(issues) > 0


class TestToolConstraints:
    """Tests for tool usage constraints."""

    def test_generate_document_requires_content(self):
        """Test generate_document tool requires content first."""
        constraints = TOOL_CONSTRAINTS.get("generate_document", {})

        assert constraints.get("requires_content_first") is True
        assert constraints.get("min_content_length", 0) >= 500

    def test_validate_tool_call_without_content(self):
        """Test tool call validation fails without sufficient content."""
        validator = ToolConstraintValidator()

        result = validator.validate_tool_call(
            tool_name="generate_document",
            arguments={"title": "Test", "content": "Short"},
            context={"generated_content_length": 100},
        )

        assert result.allowed is False
        assert "content" in result.reason.lower()

    def test_validate_tool_call_with_content(self):
        """Test tool call validation passes with sufficient content."""
        validator = ToolConstraintValidator()

        result = validator.validate_tool_call(
            tool_name="generate_document",
            arguments={"title": "Test", "content": "x" * 600},
            context={"generated_content_length": 600},
        )

        assert result.allowed is True
