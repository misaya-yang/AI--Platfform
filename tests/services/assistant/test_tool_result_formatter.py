"""
Tests for tool_result_formatter — focus on the RETRIEVAL_QUALITY signal
threshold behavior.
"""

from __future__ import annotations

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


def test_non_kb_result_has_no_unconditional_3000_character_cutoff() -> None:
    terminal_citation = "42 U.S.C. § 2000e-2(a)(1) — terminal authority"
    evidence = "Title VII evidence\n" + ("fact and authority\n" * 300) + terminal_citation
    assert len(evidence) > 3_000

    result = compact_tool_result_for_model("read_statute", evidence, {})

    assert result == evidence
    assert result.endswith(terminal_citation)


def test_kb_result_uses_token_budget_and_keeps_complete_tail_authority() -> None:
    terminal_citation = "SEC Release No. 34-100000, page 147 — terminal citation"
    content = ("liquidity disclosure evidence " * 180) + terminal_citation
    metadata = {
        "query": "material liquidity risk",
        "contexts": [
            {
                "chunks": [
                    {
                        "content": content,
                        "score": 0.91,
                        "dataset_name": "10-K",
                        "document_id": "filing-1",
                        "segment_id": "item-1a",
                    }
                ]
            }
        ],
    }

    result = compact_tool_result_for_model("search_knowledge_base", "unused", metadata)

    assert "INLINE_EVIDENCE: complete_inline" in result
    assert terminal_citation in result
    assert "stop searching" not in result.lower()


def test_kb_oversized_raw_fallback_is_explicitly_partial_and_keeps_tail() -> None:
    tail = "TAIL-AUTHORITY: 17 CFR 240.10b-5"
    raw = ("unstructured evidence " * 6_000) + tail

    result = compact_tool_result_for_model(
        "search_knowledge_base",
        raw,
        {"query": "securities fraud", "contexts": []},
    )

    assert "INLINE_EVIDENCE: partial_inline" in result
    assert "INLINE_TOOL_RESULT_STATUS: partial" in result
    assert tail in result
    assert len(result) < 100_000
