"""
Tests for tool_result_formatter — focus on the RETRIEVAL_QUALITY signal
threshold behavior.
"""

from __future__ import annotations

import pytest

from assistant_service.core.agent.tool_result_formatter import (
    _retrieval_quality_label,
    compact_tool_result_for_model,
)


# ---------------------------------------------------------------------------
# Threshold behavior
# ---------------------------------------------------------------------------


def test_quality_label_none_on_zero_results() -> None:
    assert _retrieval_quality_label(0.0, count=0) == "NONE"


def test_quality_label_high_at_08() -> None:
    assert _retrieval_quality_label(0.80, count=3) == "HIGH"
    assert _retrieval_quality_label(0.95, count=3) == "HIGH"


def test_quality_label_adequate_at_06() -> None:
    assert _retrieval_quality_label(0.60, count=3) == "ADEQUATE"
    assert _retrieval_quality_label(0.79, count=3) == "ADEQUATE"


def test_quality_label_low_under_06() -> None:
    assert _retrieval_quality_label(0.59, count=3) == "LOW"
    assert _retrieval_quality_label(0.10, count=3) == "LOW"


# ---------------------------------------------------------------------------
# Signal appears as first line in KB results
# ---------------------------------------------------------------------------


def _kb_metadata(top_score: float, count: int = 3) -> dict:
    """Synthesize KB metadata with controlled top score."""
    chunks = [
        {"content": f"chunk {i}", "score": top_score - (i * 0.1), "dataset_name": "ds1"}
        for i in range(count)
    ]
    return {
        "query": "test query",
        "contexts": [{"chunks": chunks, "dataset_name": "ds1"}],
    }


def test_retrieval_quality_signal_is_first_line() -> None:
    result = compact_tool_result_for_model(
        tool_name="search_knowledge_base",
        tool_result_text="kb",
        tool_metadata=_kb_metadata(top_score=0.85, count=3),
    )
    first_line = result.splitlines()[0]
    assert first_line.startswith("RETRIEVAL_QUALITY: HIGH")
    assert "top_score=0.85" in first_line


def test_signal_low_on_weak_match() -> None:
    result = compact_tool_result_for_model(
        tool_name="search_knowledge_base",
        tool_result_text="kb",
        tool_metadata=_kb_metadata(top_score=0.45, count=2),
    )
    assert result.startswith("RETRIEVAL_QUALITY: LOW")


def test_signal_absent_for_non_kb_tools() -> None:
    """Non-KB retrieval tools should NOT get the KB-specific quality signal."""
    result = compact_tool_result_for_model(
        tool_name="web_fetch",
        tool_result_text="web text",
        tool_metadata={"url": "https://example.com", "status": 200},
    )
    assert "RETRIEVAL_QUALITY" not in result


def test_signal_none_when_no_chunks() -> None:
    result = compact_tool_result_for_model(
        tool_name="search_knowledge_base",
        tool_result_text="",
        tool_metadata={"query": "x", "contexts": []},
    )
    # Empty contexts means no flat_chunks → top_score=0, count=0 → NONE
    assert result.startswith("RETRIEVAL_QUALITY: NONE")
