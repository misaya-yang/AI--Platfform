"""Tests for per-turn retrieval-tool deduplication helpers.

Covers both the existing KB dedup and the newly-added web-search dedup —
the driver for the 60s / 5-call NBA-score incident.
"""

from __future__ import annotations

import pytest

from assistant_service.core.agent.tool_dedup import (
    KB_REUSE_MESSAGE,
    KBDedupState,
    WEB_SEARCH_REUSE_MESSAGE,
    WebSearchDedupState,
    is_web_search_tool,
    web_query_fingerprint,
)


# ---------------------------------------------------------------------------
# Fingerprint normalization
# ---------------------------------------------------------------------------


def test_web_fingerprint_normalizes_case_and_whitespace() -> None:
    fp_a = web_query_fingerprint({"query": "  NBA Scores 2026-04-20 "})
    fp_b = web_query_fingerprint({"query": "nba scores 2026-04-20"})
    assert fp_a == fp_b
    assert fp_a == "nba scores 2026 04 20"


def test_web_fingerprint_strips_punctuation_cjk_and_ascii() -> None:
    fp_a = web_query_fingerprint({"query": "NBA，昨天的比分？"})
    fp_b = web_query_fingerprint({"query": "NBA 昨天的比分"})
    assert fp_a == fp_b


def test_web_fingerprint_empty_query_returns_empty_string() -> None:
    assert web_query_fingerprint({}) == ""
    assert web_query_fingerprint({"query": ""}) == ""
    assert web_query_fingerprint({"query": "   "}) == ""


def test_web_fingerprint_non_dict_returns_empty() -> None:
    assert web_query_fingerprint(None) == ""  # type: ignore[arg-type]
    assert web_query_fingerprint("hello") == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# is_web_search_tool — alias coverage
# ---------------------------------------------------------------------------


def test_is_web_search_tool_accepts_both_aliases() -> None:
    assert is_web_search_tool("search_web") is True
    assert is_web_search_tool("web_search") is True


def test_is_web_search_tool_rejects_other_tools() -> None:
    assert is_web_search_tool("search_knowledge_base") is False
    assert is_web_search_tool("web_fetch") is False
    assert is_web_search_tool("") is False


# ---------------------------------------------------------------------------
# WebSearchDedupState
# ---------------------------------------------------------------------------


def test_web_dedup_first_call_not_skipped() -> None:
    state = WebSearchDedupState()
    skip, reason = state.should_skip("search_web", "nba scores 2026 04 20")
    assert skip is False
    assert reason is None


def test_web_dedup_same_query_twice_short_circuits_second() -> None:
    """The exact scenario from the NBA incident: model issues the same
    normalized query twice in one turn."""
    state = WebSearchDedupState()
    fp = web_query_fingerprint({"query": "NBA yesterday scores"})
    state.mark_completed(fp)
    skip, reason = state.should_skip("search_web", fp)
    assert skip is True
    assert reason == "duplicate_query"


def test_web_dedup_near_duplicate_query_caught_via_normalization() -> None:
    """Model rewording with trivial differences (punctuation, casing,
    whitespace) should hit the same fingerprint and get deduped."""
    state = WebSearchDedupState()
    fp1 = web_query_fingerprint({"query": "NBA scores 2026-04-20"})
    fp2 = web_query_fingerprint({"query": "  nba   scores   2026-04-20  "})
    state.mark_completed(fp1)
    skip, _ = state.should_skip("search_web", fp2)
    assert skip is True


def test_web_dedup_genuinely_different_query_passes() -> None:
    state = WebSearchDedupState()
    state.mark_completed(web_query_fingerprint({"query": "NBA scores 2026-04-20"}))
    skip, _ = state.should_skip(
        "search_web",
        web_query_fingerprint({"query": "Lakers roster 2026"}),
    )
    assert skip is False


def test_web_dedup_ignores_non_web_tools() -> None:
    state = WebSearchDedupState()
    state.mark_completed("some fp")
    # Pass same fingerprint but a different tool name — must not dedup.
    skip, reason = state.should_skip("search_knowledge_base", "some fp")
    assert skip is False
    assert reason is None


def test_web_dedup_empty_fingerprint_never_matches() -> None:
    """Defensive: empty-fingerprint queries must not all collapse into one
    bucket, even if someone calls mark_completed with empty string."""
    state = WebSearchDedupState()
    state.mark_completed("")
    skip, _ = state.should_skip("search_web", "")
    assert skip is False


def test_web_dedup_handles_web_search_alias() -> None:
    state = WebSearchDedupState()
    fp = web_query_fingerprint({"query": "foo bar"})
    state.mark_completed(fp)
    # The other alias must also be recognized.
    skip, _ = state.should_skip("web_search", fp)
    assert skip is True


# ---------------------------------------------------------------------------
# KBDedupState — guard existing behavior (no regressions)
# ---------------------------------------------------------------------------


def test_kb_dedup_first_call_not_skipped() -> None:
    state = KBDedupState()
    skip, reason = state.should_skip("search_knowledge_base", "q=x|intent=general|datasets=")
    assert skip is False
    assert reason is None


def test_kb_dedup_second_call_short_circuits_as_already_completed() -> None:
    state = KBDedupState()
    state.mark_completed("q=x|intent=general|datasets=")
    skip, reason = state.should_skip(
        "search_knowledge_base", "q=y|intent=general|datasets="
    )
    assert skip is True
    # KB caps at one call per turn regardless of fingerprint.
    assert reason in {"already_completed", "duplicate_fingerprint"}


def test_kb_dedup_ignores_non_kb_tools() -> None:
    state = KBDedupState()
    skip, reason = state.should_skip("search_web", "q=whatever")
    assert skip is False
    assert reason is None


# ---------------------------------------------------------------------------
# Reuse messages — sanity checks
# ---------------------------------------------------------------------------


def test_reuse_messages_are_nonempty_strings() -> None:
    assert isinstance(KB_REUSE_MESSAGE, str) and KB_REUSE_MESSAGE.strip()
    assert isinstance(WEB_SEARCH_REUSE_MESSAGE, str) and WEB_SEARCH_REUSE_MESSAGE.strip()


def test_web_reuse_message_guides_model_toward_different_keywords() -> None:
    """The short-circuit message must nudge the model toward either
    committing to an answer or issuing a genuinely-different query —
    otherwise it'll just retry variations forever."""
    msg = WEB_SEARCH_REUSE_MESSAGE.lower()
    assert "different" in msg or "keyword" in msg


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
