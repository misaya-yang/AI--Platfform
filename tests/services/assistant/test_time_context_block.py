"""Tests for ``get_time_context_block``.

Guards against regressions that led to the NBA-score incident where the
model wasted ~60s + 5 web searches doubting the date ("this appears to be
a future date, I'll add a disclaimer…"). The time block must now assert
authoritatively and explicitly instruct the model not to second-guess it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from src.services.assistant.prompts.system_prompt_v2 import get_time_context_block


def test_time_block_contains_iso_date_for_today() -> None:
    block = get_time_context_block()
    today_iso = datetime.now().strftime("%Y-%m-%d")
    assert today_iso in block


def test_time_block_contains_explicit_yesterday_date() -> None:
    """The model must not have to do calendar arithmetic — provide the
    literal yesterday date so "yesterday" queries can be rewritten as
    dated web searches."""
    block = get_time_context_block()
    yesterday_iso = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert yesterday_iso in block


def test_time_block_contains_two_days_ago_date() -> None:
    block = get_time_context_block()
    two_days_ago_iso = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    assert two_days_ago_iso in block


def test_time_block_asserts_no_doubt_instruction() -> None:
    """Must explicitly tell the model NOT to question the date — this is
    the direct fix for the observed CoT where the model wrote 'the system
    prompt forces the date to 2026, I'll add a disclaimer'."""
    block = get_time_context_block()
    assert "NOT" in block, "expected a do-NOT-question-the-date instruction"


def test_time_block_discourages_future_date_disclaimers() -> None:
    """The CoT explicitly mentioned 'future date' doubts. The block must
    head off that failure mode."""
    block = get_time_context_block().lower()
    assert "future" in block or "disclaimer" in block


def test_time_block_includes_web_search_guidance_with_dated_example() -> None:
    """The block should teach the model to put literal dates into
    search_web queries, not vague words like 'yesterday'."""
    block = get_time_context_block()
    assert "search_web" in block
    # Must show a literal YYYY-MM-DD somewhere as an example.
    assert re.search(r"\d{4}-\d{2}-\d{2}", block) is not None


def test_time_block_is_stable_string() -> None:
    """Sanity: two calls within the same second return identical strings
    — otherwise KV-cache behavior would be unpredictable. (This can flake
    across a midnight boundary, which is fine.)"""
    a = get_time_context_block()
    b = get_time_context_block()
    assert a == b
