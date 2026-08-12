"""Tests for per-turn retrieval-tool deduplication.

PR-2 deleted the in-tree ``search_web`` (Tavily) tool — capable models do
their own web search via native APIs (Qwen ``enable_search``, Anthropic
``web_search_20250305``) and ``web_fetch`` is the URL-fetch fallback.
The web-search dedup state was removed alongside; only KB dedup remains.
"""

from __future__ import annotations

import pytest
from assistant_service.core.agent.tool_dedup import (
    KB_REUSE_MESSAGE,
    KBDedupState,
)


def test_kb_dedup_first_call_not_skipped() -> None:
    state = KBDedupState()
    skip, reason = state.should_skip("search_knowledge_base", "q=x|intent=general|datasets=")
    assert skip is False
    assert reason is None


def test_kb_dedup_allows_different_followup_query_after_success() -> None:
    state = KBDedupState()
    state.mark_completed("q=x|intent=general|datasets=")
    skip, reason = state.should_skip("search_knowledge_base", "q=y|intent=general|datasets=")
    assert skip is False
    assert reason is None


def test_kb_dedup_ignores_non_kb_tools() -> None:
    state = KBDedupState()
    skip, reason = state.should_skip("web_fetch", "q=whatever")
    assert skip is False
    assert reason is None


def test_kb_dedup_duplicate_fingerprint_caught() -> None:
    """Same KB query fingerprint twice — even before the first completes —
    must short-circuit so we don't issue redundant retrieval."""
    state = KBDedupState()
    fp = "q=foo|intent=general|datasets="
    state.mark_completed(fp)
    skip, reason = state.should_skip("search_knowledge_base", fp)
    assert skip is True
    assert reason == "duplicate_fingerprint"


def test_kb_reuse_message_is_nonempty_string() -> None:
    assert isinstance(KB_REUSE_MESSAGE, str) and KB_REUSE_MESSAGE.strip()
    assert "answer now" not in KB_REUSE_MESSAGE.lower()
    assert "only call" not in KB_REUSE_MESSAGE.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
